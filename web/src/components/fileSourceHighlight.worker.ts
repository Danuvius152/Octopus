import hljs from "highlight.js";

self.onmessage = ({ data: { text, language } }: MessageEvent<{ text: string; language: string | null }>) => {
  const result = language
    ? hljs.highlight(text, { language, ignoreIllegals: true })
    : hljs.highlightAuto(text);
  self.postMessage(result.value);
};
