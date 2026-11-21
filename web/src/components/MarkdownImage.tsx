import { Fragment, useRef, useState, type ReactNode } from "react";
import { IconExternalLink, IconReload } from "@tabler/icons-react";
import type { Element } from "hast";
import type { Components, ExtraProps, Options } from "react-markdown";

const iconClass = "absolute bottom-2 inline-flex size-8 items-center justify-center rounded-md border border-border bg-background text-foreground shadow-sm";
const focusClass = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary";
const imageWrapperTags = new Set(["a", "em", "strong", "del"]);
const collapsibleWhitespace = /^[\t\n\f\r ]*$/;
const imagePreviewTag = "markdown-image-preview";

interface ParagraphImage {
  image: Element;
  authoredLink: boolean;
}

function standaloneImage(children: Element["children"]): ParagraphImage | undefined {
  let onlyElement: Element | undefined;
  for (const child of children) {
    if (child.type === "text" && collapsibleWhitespace.test(child.value)) continue;
    if (child.type !== "element" || onlyElement) return;
    onlyElement = child;
  }
  if (!onlyElement) return;
  if (onlyElement.tagName === "img") return { image: onlyElement, authoredLink: false };
  if (imageWrapperTags.has(onlyElement.tagName)) {
    const nested = standaloneImage(onlyElement.children);
    if (nested) return { image: nested.image, authoredLink: nested.authoredLink || onlyElement.tagName === "a" };
  }
}

const paragraphImages = new WeakMap<Element, ParagraphImage>();

function ImagePreview({ src, alt, authoredLink, children }: {
  src: string; alt: string; authoredLink: boolean; children: ReactNode;
}) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const original = useRef<HTMLAnchorElement>(null);
  const description = alt.trim();
  const label = description ? `Open original image: ${description}` : "Open original image";
  const retryLabel = description ? `Retry image: ${description}` : "Retry image";
  const media = <Fragment key={attempt}>{children}</Fragment>;

  return <span className="markdown-image-preview relative block aspect-[8/5] w-full max-w-lg rounded-md border border-border bg-muted/40"
    onErrorCapture={() => setFailed(true)}>
    {authoredLink && <span className="markdown-image-author-link">{media}</span>}
    <a ref={original} href={src} target="_blank" rel="noreferrer" aria-label={label} title={label}
      className={`${authoredLink ? `${iconClass} right-2` : "absolute inset-0 rounded-md"} ${focusClass}`}>
      {!authoredLink && media}
      <span className={authoredLink ? undefined : `${iconClass} right-2`} aria-hidden="true">
        <IconExternalLink size={16} />
      </span>
    </a>
    {failed && <button type="button" className={`${iconClass} right-12 cursor-pointer ${focusClass}`}
      aria-label={retryLabel} title={retryLabel} onClick={() => {
        original.current?.focus({ preventScroll: true });
        setFailed(false);
        setAttempt((value) => value + 1);
      }}>
      <IconReload size={16} aria-hidden="true" />
    </button>}
  </span>;
}

function renderImagePreview({ node, children }: ExtraProps & { children?: ReactNode }) {
  const media = node && paragraphImages.get(node);
  // The image element's source has already passed react-markdown's URL filter.
  const src = media?.image.properties.src;
  if (!media || typeof src !== "string" || !src) return <>{children}</>;
  const alt = media.image.properties.alt;
  return <ImagePreview key={src} src={src} alt={typeof alt === "string" ? alt : ""} authoredLink={media.authoredLink}>
    {children}
  </ImagePreview>;
}

const components: Components & { [imagePreviewTag]: typeof renderImagePreview } = {
  [imagePreviewTag]: renderImagePreview,
};

export const markdownImageOptions: Pick<Options, "remarkRehypeOptions" | "components"> = {
  components,
  remarkRehypeOptions: {
    handlers: {
      paragraph(state, node) {
        const paragraph: Element = { type: "element", tagName: "p", properties: {}, children: state.all(node) };
        state.patch(node, paragraph);
        state.applyData(node, paragraph);
        const media = standaloneImage(paragraph.children);
        if (media) {
          // Mark the paragraph's content before tight list items unwrap the p.
          // Keep the original nodes in order for transcript offset mapping.
          const preview: Element = { type: "element", tagName: imagePreviewTag, properties: {}, children: paragraph.children };
          paragraphImages.set(preview, media);
          paragraph.children = [preview];
        }
        return paragraph;
      },
    },
  },
};
