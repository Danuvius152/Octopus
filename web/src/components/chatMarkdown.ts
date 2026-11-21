import Markdown, { defaultUrlTransform } from "react-markdown";
import katex from "katex";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { parse, postprocess, preprocess, type Options } from "micromark";
import { decodeString } from "micromark-util-decode-string";
import type { Element as HastElement, Root } from "hast";

export const chatRemarkPlugins = [remarkGfm, remarkMath];
export const chatRehypePlugins = [rehypeKatex];
const mathmlClass = "katex-mathml";
export const clippedMathmlSelector = `.${mathmlClass}`;
export const formulaBodySelector = ".katex-html";
export const formulaProbeOptions = { strict: "ignore", throwOnError: false, trust: false } as const;

interface TextSpan { start: number; end: number }
interface SourceToken extends TextSpan { kind: string; text: string }
interface FormulaSource { source: string; displayMode: boolean }
interface Formula extends FormulaSource { position: TextSpan; glyphs: { start: number; text: string }[] }
type CodeKind = "inline" | "block";

const mathTokens = new Set(["mathTextData", "mathFlowValue"]);
const renderedTokens = new Set(["data", "characterEscape", "characterReference", "lineEnding", "codeTextData", "codeFlowValue", "codeFencedFence", ...mathTokens]);

function markdownTokens(source: string, extensions: Options["extensions"]) {
  const events = postprocess(parse({ extensions }).document().write(preprocess()(source, "utf8", true)));
  return events.flatMap(([event, token, context]): SourceToken[] => {
    if (event !== "enter" || !renderedTokens.has(token.type)) return [];
    const raw = context.sliceSerialize(token);
    const start = token.start.offset;
    if (token.type === "lineEnding") {
      // A line-ending token can extend into the next quote/list prefix.
      const text = /^(?:\r\n|\r|\n)/.exec(raw)?.[0];
      return text ? [{ kind: token.type, text, start, end: start + text.length }] : [];
    }
    const text = token.type === "characterEscape" || token.type === "characterReference" ? decodeString(raw) : raw;
    return [{ kind: token.type, start, end: token.end.offset, text }];
  });
}

function spansFromTokens(tokens: SourceToken[], position: TextSpan, value: string, added: TextSpan, code?: CodeKind): TextSpan[] {
  let parts = tokens.flatMap((token) => {
    const start = Math.max(token.start, position.start);
    const end = Math.min(token.end, position.end);
    if (end <= start) return [];
    if (token.start === start && token.end === end) return [token];
    // A task-list text token can also contain the space after its checkbox.
    // Clip that space without splitting a decoded entity or transformed token.
    if (token.end - token.start !== token.text.length) return [];
    return [{ ...token, start, end, text: token.text.slice(start - token.start, end - token.start) }];
  });
  if (code === "block") {
    const fence = parts.find((token) => token.kind === "codeFencedFence");
    const openingEnd = fence && parts.find((token) => token.kind === "lineEnding" && token.start >= fence.end)?.end;
    parts = parts.filter((token) => (!fence || token.start >= (openingEnd ?? position.end)) &&
      (token.kind === "codeFlowValue" || token.kind === "lineEnding"));
    const last = parts.at(-1);
    if (last?.kind === "lineEnding") parts[parts.length - 1] = { ...last, text: "\n" };
  } else if (code === "inline") {
    parts = parts.filter((token) => token.kind === "codeTextData" || token.kind === "lineEnding")
      .map((token) => token.kind === "lineEnding" ? { ...token, text: " " } : token);
  } else parts = parts.filter((token) =>
    token.kind !== "codeTextData" && token.kind !== "codeFlowValue" && token.kind !== "codeFencedFence" && !mathTokens.has(token.kind));

  const text = parts.map((part) => part.text).join("");
  // The renderer can append a code-block newline with no source character.
  if (text !== value && !(code === "block" && text + "\n" === value)) return [];
  let offset = 0;
  const spans: TextSpan[] = [];
  for (const part of parts) {
    if (part.start < added.end && added.start < part.end) {
      const direct = part.end - part.start === part.text.length;
      const start = direct ? Math.max(0, added.start - part.start) : 0;
      const end = direct ? Math.min(part.text.length, added.end - part.start) : part.text.length;
      if (part.text.slice(start, end).trim()) spans.push({ start: offset + start, end: offset + end });
    }
    offset += part.text.length;
  }
  return spans;
}

function isMathElement(node: HastElement): boolean {
  const classes = node.properties.className;
  return Array.isArray(classes) && classes.some((name) => name === "math-inline" || name === "math-display" || name === "language-math");
}

function transparentFormulaText(style: string | null | undefined, inherited: boolean) {
  const color = /(?:^|;)\s*color:\s*([^;]+)/.exec(style ?? "")?.[1]?.trim();
  return color ? /^(?:transparent|#[\da-f]{3}0|#[\da-f]{6}00|rgba\(.*,\s*0(?:\.0+)?\))$/i.test(color) : inherited;
}

function visibleFormulaText(element: Element, inherited = false): string {
  const transparent = transparentFormulaText(element.getAttribute("style"), inherited);
  return Array.from(element.childNodes, (node) => {
    if (node instanceof Element) return visibleFormulaText(node, transparent);
    return !transparent && node.nodeType === Node.TEXT_NODE ? node.textContent ?? "" : "";
  }).join("");
}

function literalFormulaSpans(formula: Formula, tokens: SourceToken[], selected: TextSpan): TextSpan[] {
  const { position, glyphs, source, displayMode } = formula;
  const markdown = tokens.filter((token) => (mathTokens.has(token.kind) || token.kind === "codeFlowValue") &&
    position.start <= token.start && token.end <= position.end)
    .flatMap((token) => Array.from(token.text.matchAll(/\S/g), (match) => ({ value: match[0], offset: token.start + match.index })));
  const tex = Array.from(source.matchAll(/\S/g));
  if (tex.length !== markdown.length || tex.some((match, index) => match[0] !== markdown[index].value)) return [];
  const selectedIndices = markdown.flatMap(({ offset }, index) => selected.start <= offset && offset < selected.end ? [index] : []);
  const firstIndex = selectedIndices[0];
  const lastIndex = selectedIndices.at(-1);
  if (firstIndex === undefined || lastIndex === undefined) return [];
  const target = glyphs.flatMap(({ start, text }) =>
    Array.from(text.matchAll(/[^\s\u200b]/g), (match) => ({ value: match[0], offset: start + match.index })));
  const glyphSpan = (index: number) => ({ start: target[index].offset, end: target[index].offset + 1 });
  // With no TeX commands or structural tokens, an exact literal match
  // establishes the order. Structured formulas can reorder identical glyphs.
  if (!/[\\{}^_%&#$~']/.test(source) && target.length === tex.length &&
    target.every(({ value }, index) => value === tex[index][0])) return selectedIndices.map(glyphSpan);

  const first = tex[firstIndex].index;
  const last = tex[lastIndex].index;
  const replacements: { before: string; after: string }[] = [];
  let unsupported = false;
  let controlWordEnd = -1;
  const marked = source.replace(/(\\[A-Za-z@]+)|\\[^\r\n]|%[^\r\n]*|\S/gu, (token, controlWord: string | undefined, offset: number) => {
    if (controlWord) controlWordEnd = offset + token.length;
    if (offset > last || offset + token.length <= first || /^[{}^_]$/.test(token)) return token;
    if (/^[\\%&#$~']/.test(token)) { unsupported = true; return token; }
    const alternate = /^\d$/.test(token) ? (token === "7" ? "8" : "7") : (token === "z" ? "q" : "z");
    replacements.push({ before: token, after: alternate });
    // KaTeX consumes spaces after a control word. Keep a replacement letter
    // separate from the word when the original token served as its boundary.
    return offset === controlWordEnd ? ` ${alternate}` : alternate;
  });
  if (unsupported || !replacements.length) return [];
  // Render a detached copy with only the selected literals changed. Verify
  // which glyphs changed, including when the renderer reordered the source.
  const probe = document.createElement("div");
  try {
    katex.render(marked, probe, { ...formulaProbeOptions, displayMode });
  } catch {
    // A changed literal can make the formula invalid in its TeX context.
    return [];
  }
  const visual = probe.querySelector(formulaBodySelector);
  if (!visual) return [];
  const sample = visibleFormulaText(visual).replace(/[\s\u200b]/g, "");
  if (sample.length !== target.length) return [];
  const changed = target.flatMap(({ value }, index) => sample[index] === value ? [] : [index]);
  if (changed.length !== replacements.length || changed.some((index, order) =>
    index !== changed[0] + order || target[index].value !== replacements[order].before || sample[index] !== replacements[order].after)) return [];
  return changed.map(glyphSpan);
}

// Project source offsets through the same Markdown pipeline as the saved
// bubble. The browser answers where those rendered characters will land.
export function projectMarkdownOutput(source: string, added?: TextSpan, purpose: "output" | "selection" = "output") {
  const fragments: string[] = [];
  const spans: TextSpan[] = [];
  const images: number[] = [];
  const mathOutput: (FormulaSource & { index: number })[] = [];
  let textLength = 0;
  let imageCount = 0;
  let formulaCount = 0;
  let extensions: Options["extensions"];
  let tokens: SourceToken[] | undefined;
  const formulas = new WeakMap<HastElement, FormulaSource>();
  const retainMathSource = () => (root: Root) => {
    // KaTeX replaces the input element. Keep its source position on an
    // enclosing node in this projection only, with identical output text.
    const visit = (parent: Root | HastElement) => {
      for (let index = 0; index < parent.children.length; index++) {
        const child = parent.children[index];
        if (child.type !== "element") continue;
        const math = child.tagName === "pre" ? child.children.find((entry): entry is HastElement =>
          entry.type === "element" && entry.tagName === "code" && isMathElement(entry)) : isMathElement(child) ? child : undefined;
        if (math) {
          const region: HastElement = { type: "element", tagName: "span", properties: {},
            position: math.position ?? child.position, children: [child] };
          formulas.set(region, {
            source: math.children.map((entry) => entry.type === "text" ? entry.value : "").join(""),
            displayMode: child.tagName === "pre" ||
              (Array.isArray(math.properties.className) && math.properties.className.includes("math-display")),
          });
          parent.children[index] = region;
        } else visit(child);
      }
    };
    visit(root);
  };
  function readSyntax(this: { data(key: "micromarkExtensions"): unknown }) {
    extensions = this.data("micromarkExtensions") as Options["extensions"];
  }
  const inspect = () => (root: Root) => {
    const visit = (node: Root["children"][number], inherited?: TextSpan, code?: CodeKind,
      formula?: Formula, clipped = false, transparent = false) => {
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      const original = start === undefined || end === undefined ? undefined : { start, end };
      const position = formula?.position ?? original ?? inherited;
      const overlaps = !added || !!position && added.start < position.end && added.end > position.start;
      if (node.type === "element") {
        if (node.tagName === "img") {
          const { src, alt } = node.properties;
          if (overlaps && (typeof src === "string" && defaultUrlTransform(src) ||
            typeof alt === "string" && alt.trim())) images.push(imageCount);
          imageCount++;
        }
        const childCode = node.tagName === "pre" ? "block" : node.tagName === "code" ? code ?? "inline" : code;
        const classes = node.properties.className;
        const childClipped = clipped || Array.isArray(classes) && classes.includes(mathmlClass);
        const childTransparent = transparentFormulaText(typeof node.properties.style === "string" ? node.properties.style : undefined, transparent);
        const math = formulas.get(node);
        const renderedMath = math && node.children.some((child) => child.type === "element" &&
          Array.isArray(child.properties.className) && child.properties.className.some((name) => name === "katex" || name === "katex-display"));
        if (math && renderedMath) {
          if (overlaps && purpose === "output") mathOutput.push({ ...math, index: formulaCount });
          formulaCount++;
        }
        const region = position && renderedMath && math ? { ...math, position, glyphs: [] } : undefined;
        for (const child of node.children) visit(child, position, childCode, region ?? formula, childClipped, childTransparent);
        if (region && added && purpose === "selection" && region.glyphs.length) {
          tokens ??= markdownTokens(source, extensions);
          spans.push(...literalFormulaSpans(region, tokens, added));
        }
      } else if ("value" in node && typeof node.value === "string" && node.type !== "comment") {
        const value = node.value;
        if (overlaps && !clipped && !transparent) {
          if (formula) {
            if (purpose === "selection" && /[^\s\u200b]/.test(value)) formula.glyphs.push({ start: textLength, text: value });
          } else if (!added || !position || added.start <= position.start && position.end <= added.end) {
            if (value.trim()) spans.push({ start: textLength, end: textLength + value.length });
          } else if (source.slice(position.start, position.end) === value) {
            const from = Math.max(0, added.start - position.start);
            const to = Math.min(value.length, added.end - position.start);
            if (value.slice(from, to).trim()) spans.push({ start: textLength + from, end: textLength + to });
          } else if (original || code) {
            tokens ??= markdownTokens(source, extensions);
            for (const { start, end } of spansFromTokens(tokens, position, value, added, code)) {
              spans.push({ start: textLength + start, end: textLength + end });
            }
          }
        }
        fragments.push(value);
        textLength += value.length;
      }
    };
    for (const child of root.children) visit(child);
  };
  // The synchronous Markdown export runs the configured processor and returns
  // a React tree. This query only consumes the positions in the processor.
  Markdown({ children: source, remarkPlugins: [...chatRemarkPlugins, readSyntax], rehypePlugins: [retainMathSource, ...chatRehypePlugins, inspect] });
  return { text: fragments.join(""), spans, images, mathOutput };
}
