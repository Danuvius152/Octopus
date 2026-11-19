import { readFileSync, writeFileSync } from 'node:fs'
import { exit } from 'node:process'
import { html, parse } from 'parse5'

// https://mimesniff.spec.whatwg.org/#javascript-mime-type
const javascriptTypes = new Set([
  'application/ecmascript', 'application/javascript', 'application/x-ecmascript', 'application/x-javascript',
  'text/ecmascript', 'text/javascript', 'text/javascript1.0', 'text/javascript1.1', 'text/javascript1.2',
  'text/javascript1.3', 'text/javascript1.4', 'text/javascript1.5', 'text/jscript', 'text/livescript',
  'text/x-ecmascript', 'text/x-javascript',
])

function* elements(node) {
  if (node.namespaceURI === html.NS.HTML) yield node
  // Template contents live in a separate document fragment.
  for (const child of node.childNodes ?? []) yield* elements(child)
}

function isExecutableScript(attrs) {
  const type = attrs.get('type')
  const language = attrs.get('language')
  const scriptType = (type ?? (language ? 'text/' + language : '')).toLowerCase()
  if (scriptType === 'module') return true
  const mime = type === undefined ? scriptType : scriptType.replace(/^[\t\n\f\r ]+|[\t\n\f\r ]+$/g, '')
  return !attrs.has('nomodule') && (scriptType === '' || javascriptTypes.has(mime))
}

function parseURL(value, base) {
  try {
    return new URL(value, base)
  } catch {
    return null
  }
}

function documentBase(page, href) {
  const base = href === undefined ? null : parseURL(href, page)
  return base && base.protocol !== 'data:' && base.protocol !== 'javascript:' ? base : page
}

const source = new TextDecoder('utf-8', { fatal: true }).decode(readFileSync(0))
const document = parse(source, { scriptingEnabled: true, sourceCodeLocationInfo: true })
const references = []
let baseTag
for (const { tagName, attrs: attributes, sourceCodeLocation } of elements(document)) {
  const attrs = new Map(attributes.map(({ name, value }) => [name, value]))
  if (tagName === 'base' && attrs.has('href') && !baseTag) {
    baseTag = { href: attrs.get('href'), offset: sourceCodeLocation.startOffset }
  } else if (tagName === 'script' && attrs.get('src')) {
    references.push({
      reference: attrs.get('src'), executable: isExecutableScript(attrs), offset: sourceCodeLocation.startOffset,
    })
  } else if (tagName === 'link' && attrs.get('href')) {
    const rel = (attrs.get('rel') ?? '').toLowerCase().split(/[\t\n\f\r ]+/)
    if (rel.includes('stylesheet') || rel.includes('modulepreload')) {
      references.push({ reference: attrs.get('href'), executable: false, offset: sourceCodeLocation.startOffset })
    }
  }
}

// A browser may already have requested an asset before it encounters the base element.
if (baseTag && references.some(({ offset }) => offset < baseTag.offset)) {
  writeFileSync(2, "The entry document's <base href> must precede the candidate asset references.\n")
  exit(1)
}

// A local path resolves to the document's own origin on both HTTP and HTTPS.
const pages = [new URL('http://release.invalid/'), new URL('https://candidate.invalid/')]
const bases = pages.map(page => documentBase(page, baseTag?.href))
const localAssets = []
for (const { reference, executable } of references) {
  const urls = bases.map(base => parseURL(reference, base))
  if (urls.every((url, index) => url?.origin === pages[index].origin) && urls[0].pathname === urls[1].pathname) {
    localAssets.push([reference, urls[0].pathname, executable])
  }
}
writeFileSync(1, JSON.stringify(localAssets))
