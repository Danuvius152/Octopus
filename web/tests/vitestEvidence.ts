import { execFileSync } from 'node:child_process'
import { channel } from 'node:diagnostics_channel'
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { inspect } from 'node:util'
import type { RunnerTaskResultPack, TestError } from 'vitest'
import type { Reporter, TestCase, TestModule, TestSpecification, TestSuite, Vitest } from 'vitest/node'

const capability = 'octopus.vitest.evidence.v4'
const attributedFailures = channel(capability + '.failure')
const taskUpdates = channel(capability + '.tasks')

type AttributedFailure = {
  error: Error | (TestError & { type?: string })
  project: string
} & ({
  phase: 'collection'
  file: string
} | {
  phase: 'worker'
  files: string[]
  pool: string
  poolId: number
  workerId: number
})

interface TaskUpdate {
  vitest: Vitest
  packs: RunnerTaskResultPack[]
}

function errorDetails(error: unknown, seen = new Set<object>()): object {
  if (typeof error !== 'object' || error === null) return { message: String(error) }
  if (seen.has(error)) return { message: '[Circular error cause]' }
  seen.add(error)
  const { name, message, stack, stacks, diff, cause, code, loc, id, plugin } = error as Partial<TestError>
  return {
    name,
    message: typeof message === 'string' ? message : inspect(error, { customInspect: false, getters: false }),
    stack, stacks, diff, code, loc, id, plugin,
    cause: cause === undefined ? undefined : errorDetails(cause, seen),
  }
}

function persistentError(error: unknown): object {
  // Vitest's worker transport can preserve values that JSON cannot encode.
  const ancestors: object[] = []
  const json = JSON.stringify(errorDetails(error), function (this: object, _key: string, value: unknown) {
    if (typeof value === 'bigint') return `${value}n`
    if (typeof value !== 'object' || value === null) return value
    while (ancestors.length && ancestors.at(-1) !== this) ancestors.pop()
    if (ancestors.includes(value)) return '[Circular]'
    ancestors.push(value)
    return value
  })
  return JSON.parse(json)
}

function moduleDetails(module: TestModule) {
  return {
    module_id: module.id,
    file: module.moduleId,
    project: module.project.name,
  }
}

function testDetails(test: TestCase) {
  return { test_id: test.id, name: test.fullName, location: test.location }
}

export class VitestEvidence implements Reporter {
  private readonly directory: string
  private readonly observedTaskErrors = new Map<string, string[]>()
  private readonly recordedFailures = new WeakSet<object>()

  constructor(directory: string) {
    this.directory = resolve(directory)
  }

  onInit(vitest: Vitest) {
    if (vitest.config.watch) throw new Error('Vitest evidence requires a single run')
    if (!Reflect.get(vitest.constructor, Symbol.for(capability))) {
      throw new Error('Vitest evidence requires the patched dependency. Run bun install --frozen-lockfile in web/.')
    }
    const git = (...args: string[]) => execFileSync('git', args, { encoding: 'utf8' }).trim()
    const run = {
      candidate: { head: git('rev-parse', 'HEAD'), dirty: Boolean(git('status', '--porcelain')) },
      argv: process.argv,
      cwd: process.cwd(),
      root: vitest.config.root,
      config: vitest.config.config,
      max_workers: vitest.config.maxWorkers,
      shard: vitest.config.shard,
    }
    mkdirSync(this.directory, { mode: 0o700 })
    writeFileSync(join(this.directory, 'run.json'), JSON.stringify(run, null, 2) + '\n', { flag: 'wx', flush: true })
    attributedFailures.subscribe(this.#recordAttributedFailure)
    taskUpdates.subscribe(this.#recordTaskUpdate)
  }

  onTestRunStart(specifications: ReadonlyArray<TestSpecification>) {
    this.write({
      event: 'run-start',
      specifications: specifications.map(spec => ({
        module_id: spec.taskId,
        file: spec.moduleId,
        project: spec.project.name,
        pool: spec.pool,
        test_ids: spec.testIds,
        test_lines: spec.testLines,
        test_name_pattern: spec.testNamePattern?.toString(),
        test_tags: spec.testTagsFilter,
      })),
    })
  }

  onTestModuleQueued(module: TestModule) {
    this.write({ event: 'queued', ...moduleDetails(module) })
  }

  onTestModuleCollected(module: TestModule) {
    this.write({
      event: 'collected',
      ...moduleDetails(module),
      tests: Array.from(module.children.allTests(), test => ({ ...testDetails(test), mode: test.options.mode })),
    })
    this.recordModuleErrors(module, 'collection')
    for (const suite of module.children.allSuites()) this.recordModuleErrors(suite, 'collection')
  }

  onTestCaseResult(test: TestCase) {
    const { state } = test.result()
    if (state === 'pending') return
    const diagnostic = test.diagnostic()
    this.write({
      event: 'test-result',
      phase: 'test',
      ...moduleDetails(test.module),
      ...testDetails(test),
      state,
      retry_count: diagnostic?.retryCount,
      repeat_count: diagnostic?.repeatCount,
    })
  }

  onTestModuleEnd(module: TestModule) {
    this.recordModuleErrors(module, 'module')
  }

  onTestSuiteResult(suite: TestSuite) {
    this.recordModuleErrors(suite, 'suite')
  }

  onTestRunEnd() {
    attributedFailures.unsubscribe(this.#recordAttributedFailure)
    taskUpdates.unsubscribe(this.#recordTaskUpdate)
  }

  // Observe the received snapshots before Vitest dispatches hook and result callbacks.
  #recordTaskUpdate = (message: unknown) => {
    const { vitest, packs } = message as TaskUpdate
    try {
      for (const [id, result] of packs) {
        if (!result?.errors?.length) continue
        const entity = vitest.state.getReportedEntityById(id)
        if (!entity) continue
        this.recordTaskErrors(entity, entity.type, result.errors, {
          retry_count: result.retryCount,
          repeat_count: result.repeatCount,
        })
      }
    } catch (error) {
      this.reportWriteError(error)
    }
  }

  // The pinned pool publishes the native error while its origin is available.
  #recordAttributedFailure = (message: unknown) => {
    const failure = message as AttributedFailure
    const { error, phase, project } = failure
    if (this.recordedFailures.has(error)) return
    const origin = failure.phase === 'worker'
      ? {
          file: failure.files.length === 1 ? failure.files[0] : undefined,
          files: failure.files,
          pool: failure.pool,
          pool_id: failure.poolId,
          worker_id: failure.workerId,
        }
      : { file: failure.file }
    const type = 'type' in error ? error.type : undefined
    if (this.writeFailure({ phase, type, project, ...origin }, error)) {
      this.recordedFailures.add(error)
    }
  }

  // Vitest also reports unhandled errors in its native summary. Observe them
  // here as they reach the coordinator's error callback.
  readonly onUnhandledError = (error: (TestError | Error) & { type: string }) => {
    if (this.recordedFailures.has(error)) return
    const { VITEST_TEST_PATH: file, VITEST_TEST_NAME: name } = error as Partial<TestError>
    this.writeFailure({ phase: 'unhandled', type: error.type, file, name }, error)
  }

  private recordModuleErrors(entity: TestModule | TestSuite, phase: 'collection' | 'module' | 'suite') {
    this.recordTaskErrors(entity, phase, entity.errors())
  }

  private recordTaskErrors(
    entity: TestCase | TestModule | TestSuite,
    phase: 'collection' | 'test' | 'module' | 'suite',
    errors: ReadonlyArray<TestError>,
    counts?: { retry_count?: number; repeat_count?: number },
  ) {
    if (!errors.length) return
    const details = errors.map(persistentError)
    // Snapshots usually grow, but test.fails can replace the array. Parsed
    // stack frames are added by the coordinator and are not part of its identity.
    const keys = details.map(error => JSON.stringify(error, (key, value) => key === 'stacks' ? undefined : value))
    const previous = this.observedTaskErrors.get(entity.id) ?? []
    const changed = keys.findIndex((key, index) => key !== previous[index])
    if (changed !== -1) {
      const module = entity.type === 'module' ? entity : entity.module
      this.write({
        event: 'error',
        phase,
        ...moduleDetails(module),
        ...(entity.type === 'test' ? testDetails(entity) : {
          name: entity.type === 'suite' ? entity.fullName : undefined,
          location: entity.location,
        }),
        ...counts,
        errors: details.slice(changed),
      })
    }
    this.observedTaskErrors.set(entity.id, keys)
  }

  private write(event: object) {
    appendFileSync(join(this.directory, 'events.jsonl'), JSON.stringify(event) + '\n', { flush: true })
  }

  private writeFailure(fields: object, error: unknown) {
    try {
      this.write({ event: 'error', ...fields, errors: [persistentError(error)] })
      return true
    } catch (writeError) {
      this.reportWriteError(writeError)
      return false
    }
  }

  private reportWriteError(error: unknown) {
    // A reporter write error can itself reach the unhandled error callback.
    console.error('Could not persist Vitest failure evidence:', error)
    process.exitCode = 1
  }
}
