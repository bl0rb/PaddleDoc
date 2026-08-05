'use client';

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { ImageOff } from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';

import { apiFetch } from '@/lib/api';

/** Artifact metadata as returned by GET /api/v1/jobs/{id}/artifacts. */
export type JobArtifact = {
  id: string;
  kind: string;
  filename: string;
  content_type: string;
  size_bytes: number;
};

const ARTIFACT_PREFIX = 'artifacts/';

/**
 * Restrict link/image URLs to https:, http:, in-page #fragments, relative
 * artifacts/… and internal /jobs/… — everything else (javascript:, data:,
 * vbscript:, protocol-relative //host, other relatives) is dropped.
 */
function urlTransform(url: string): string | null {
  if (/^https?:\/\//i.test(url)) {
    return url;
  }
  if (url.startsWith('#')) {
    // The importer deliberately preserves fragment-only hrefs (Confluence
    // TOC macros, intra-page anchors).
    return url;
  }
  if (url.startsWith('/jobs/')) {
    return url;
  }
  if (url.startsWith(ARTIFACT_PREFIX)) {
    return url;
  }
  return null;
}

function isExternalUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

function artifactContentPath(jobId: string, artifactId: string, password?: string): string {
  const passwordQS = password ? `?password=${encodeURIComponent(password)}` : '';
  return `/api/v1/jobs/${jobId}/artifacts/${artifactId}/content${passwordQS}`;
}

/** Resolve an `artifacts/{filename}` src against the job's artifact list. */
function resolveArtifact(artifacts: JobArtifact[] | null, src: string): JobArtifact | null {
  if (!artifacts) {
    return null;
  }
  const raw = src.slice(ARTIFACT_PREFIX.length);
  let decoded = raw;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    // Malformed percent-encoding — fall back to the raw name.
  }
  return (
    artifacts.find((artifact) => artifact.filename === raw) ??
    artifacts.find((artifact) => artifact.filename === decoded) ??
    null
  );
}

function BrokenImagePlaceholder({ label }: { label: string }) {
  return (
    <span className="my-2 inline-flex max-w-full items-center gap-2 rounded-md border border-dashed border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-500">
      <ImageOff className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="truncate">{label}</span>
    </span>
  );
}

function ImageSkeleton({ filename }: { filename: string }) {
  return (
    <span
      className="my-2 block h-40 w-full max-w-md animate-pulse rounded-md bg-slate-100"
      role="status"
      aria-label={`Loading image ${filename}`}
    />
  );
}

/**
 * Image stored as a job artifact (D10): fetched with credentials through
 * apiFetch (naked <img src> cannot carry the session cookie cross-origin
 * or the per-job password), served via an object URL, revoked on unmount.
 */
function ArtifactImage({
  jobId,
  password,
  artifacts,
  src,
  alt,
}: {
  jobId?: string;
  password?: string;
  /** null while the artifact list is still loading (skeleton, not error). */
  artifacts: JobArtifact[] | null;
  src: string;
  alt?: string;
}) {
  const artifact = useMemo(() => resolveArtifact(artifacts, src), [artifacts, src]);
  const artifactId = artifact?.id;
  const filename = artifact?.filename ?? src.slice(ARTIFACT_PREFIX.length);
  // Result keyed by artifact id: a stale entry (key mismatch) is simply
  // ignored, so the effect never needs a synchronous state reset.
  const [result, setResult] = useState<{ key: string; url: string | null; failed: boolean } | null>(null);

  useEffect(() => {
    if (!jobId || !artifactId) {
      return;
    }
    let cancelled = false;
    let createdUrl: string | null = null;

    const load = async () => {
      try {
        const response = await apiFetch(artifactContentPath(jobId, artifactId, password), {
          cache: 'no-store',
          skipAuthRedirect: true,
        });
        if (cancelled) {
          return;
        }
        if (!response.ok) {
          setResult({ key: artifactId, url: null, failed: true });
          return;
        }
        const blob = await response.blob();
        if (cancelled) {
          return;
        }
        createdUrl = URL.createObjectURL(blob);
        setResult({ key: artifactId, url: createdUrl, failed: false });
      } catch {
        if (!cancelled) {
          setResult({ key: artifactId, url: null, failed: true });
        }
      }
    };
    void load();

    return () => {
      cancelled = true;
      if (createdUrl) {
        URL.revokeObjectURL(createdUrl);
      }
    };
  }, [jobId, artifactId, password]);

  const current = result && result.key === artifactId ? result : null;
  if (jobId && artifacts === null) {
    // Artifact list not fetched yet — loading, not an error (§6.3).
    return <ImageSkeleton filename={filename} />;
  }
  if (!jobId || !artifact || current?.failed) {
    return <BrokenImagePlaceholder label={filename} />;
  }
  const objectUrl = current?.url ?? null;
  if (!objectUrl) {
    return <ImageSkeleton filename={filename} />;
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- object URL from an authenticated fetch; next/image cannot load blob: sources
    <img
      src={objectUrl}
      alt={alt || filename}
      className="my-2 h-auto max-w-full rounded-md border border-slate-200"
    />
  );
}

/**
 * External-host image: never auto-fetched (no automatic third-party
 * requests from a viewer's browser) — click-to-load placeholder instead.
 */
function ExternalImage({ src, alt }: { src: string; alt?: string }) {
  const [load, setLoad] = useState(false);

  let host = '';
  try {
    host = new URL(src).hostname;
  } catch {
    return <BrokenImagePlaceholder label={src} />;
  }

  if (load) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- user-approved external host; domains are unknown at build time so next/image cannot be configured for them
      <img
        src={src}
        alt={alt || host}
        loading="lazy"
        referrerPolicy="no-referrer"
        className="my-2 h-auto max-w-full rounded-md border border-slate-200"
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => setLoad(true)}
      className="my-2 inline-flex max-w-full items-center gap-2 rounded-md border border-dashed border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-600 hover:border-emerald-300 hover:bg-emerald-50"
    >
      <ImageOff className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="truncate">Load external image from {host}</span>
    </button>
  );
}

/**
 * Link to a stored artifact (`artifacts/{filename}`): a naked relative href
 * would 404 against the frontend origin, so fetch the bytes with credentials
 * on click and hand them to the browser as a download.
 */
function ArtifactLink({
  jobId,
  password,
  artifacts,
  href,
  children,
}: {
  jobId?: string;
  password?: string;
  /** null while the artifact list is still loading. */
  artifacts: JobArtifact[] | null;
  href: string;
  children?: ReactNode;
}) {
  const artifact = useMemo(() => resolveArtifact(artifacts, href), [artifacts, href]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!jobId || !artifact) {
    return <span className="text-slate-500">{children}</span>;
  }

  const download = async () => {
    setBusy(true);
    setFailed(false);
    try {
      const response = await apiFetch(artifactContentPath(jobId, artifact.id, password), {
        cache: 'no-store',
        skipAuthRedirect: true,
      });
      if (!response.ok) {
        setFailed(true);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = artifact.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        type="button"
        disabled={busy}
        onClick={() => void download()}
        className="text-emerald-700 underline underline-offset-2 hover:text-emerald-800 disabled:opacity-50"
      >
        {children}
      </button>
      {failed && (
        <span className="ml-1.5 text-xs text-red-600" role="alert">
          (download failed — click to retry)
        </span>
      )}
    </>
  );
}

const CONTAINER_CLASS = [
  'space-y-4 text-sm leading-relaxed text-slate-800 break-words',
  '[&_h1]:font-serif [&_h1]:text-2xl [&_h1]:font-semibold [&_h1]:text-slate-950 [&_h1]:mt-6 [&_h1]:first:mt-0',
  '[&_h2]:text-xl [&_h2]:font-semibold [&_h2]:text-slate-950 [&_h2]:mt-6 [&_h2]:first:mt-0',
  '[&_h3]:text-lg [&_h3]:font-semibold [&_h3]:text-slate-950 [&_h3]:mt-4',
  '[&_h4]:text-base [&_h4]:font-semibold [&_h4]:text-slate-950 [&_h4]:mt-4',
  '[&_h5]:text-sm [&_h5]:font-semibold [&_h5]:text-slate-950 [&_h6]:text-sm [&_h6]:font-semibold',
  '[&_ul]:list-disc [&_ul]:pl-6 [&_ol]:list-decimal [&_ol]:pl-6 [&_li]:my-1',
  '[&_blockquote]:border-l-4 [&_blockquote]:border-slate-200 [&_blockquote]:pl-4 [&_blockquote]:text-slate-600',
  '[&_hr]:my-6 [&_hr]:border-slate-200',
].join(' ');

/** Leading YAML frontmatter block (`---\n…\n---`) emitted by the importer. */
const FRONTMATTER_RE = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/;

export type MarkdownViewProps = {
  markdown: string;
  /** Job the markdown belongs to — required for artifact image/link fetches. */
  jobId?: string;
  /** Per-job document password, forwarded to artifact content fetches. */
  password?: string;
  /**
   * Artifact list from GET /jobs/{id}/artifacts, fetched once by the page.
   * Pass null while the fetch is in flight (artifact images render a
   * skeleton instead of the broken-image error placeholder).
   */
  artifacts?: JobArtifact[] | null;
  className?: string;
};

/**
 * Sanitized GFM renderer (D9): raw HTML is never enabled, rehype-sanitize is
 * belt-and-braces on top, and urlTransform restricts URL schemes (§5.3).
 */
export function MarkdownView({ markdown, jobId, password, artifacts = [], className }: MarkdownViewProps) {
  const body = useMemo(() => markdown.replace(FRONTMATTER_RE, ''), [markdown]);

  const components = useMemo<Components>(
    () => ({
      img: ({ src, alt }) => {
        const url = typeof src === 'string' ? src : '';
        if (!url) {
          return <BrokenImagePlaceholder label={alt || 'image unavailable'} />;
        }
        if (url.startsWith(ARTIFACT_PREFIX)) {
          return <ArtifactImage jobId={jobId} password={password} artifacts={artifacts} src={url} alt={alt} />;
        }
        if (isExternalUrl(url)) {
          return <ExternalImage src={url} alt={alt} />;
        }
        return <BrokenImagePlaceholder label={alt || url} />;
      },
      a: ({ href, children }) => {
        if (!href) {
          return <span>{children}</span>;
        }
        if (href.startsWith('#')) {
          // In-page anchor (Confluence TOC macro / intra-page link): plain
          // same-page navigation, never target="_blank".
          return (
            <a href={href} className="text-emerald-700 underline underline-offset-2 hover:text-emerald-800">
              {children}
            </a>
          );
        }
        if (href.startsWith('/jobs/')) {
          return (
            <Link href={href} className="text-emerald-700 underline underline-offset-2 hover:text-emerald-800">
              {children}
            </Link>
          );
        }
        if (href.startsWith(ARTIFACT_PREFIX)) {
          return (
            <ArtifactLink jobId={jobId} password={password} artifacts={artifacts} href={href}>
              {children}
            </ArtifactLink>
          );
        }
        return (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-emerald-700 underline underline-offset-2 hover:text-emerald-800"
          >
            {children}
          </a>
        );
      },
      code: ({ className: codeClassName, children, ...props }) => (
        <code
          className={`rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[0.875em] text-emerald-800 ${codeClassName ?? ''}`}
          {...props}
        >
          {children}
        </code>
      ),
      pre: ({ children }) => (
        <pre className="overflow-x-auto rounded-md border border-slate-200 bg-white p-4 text-sm text-emerald-800 [&>code]:block [&>code]:bg-transparent [&>code]:p-0">
          {children}
        </pre>
      ),
      table: ({ children }) => (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-sm [&_td]:border [&_td]:border-slate-200 [&_td]:px-3 [&_td]:py-2 [&_th]:border [&_th]:border-slate-200 [&_th]:bg-slate-50 [&_th]:px-3 [&_th]:py-2 [&_th]:font-semibold">
            {children}
          </table>
        </div>
      ),
    }),
    [jobId, password, artifacts]
  );

  return (
    <div className={className ? `${CONTAINER_CLASS} ${className}` : CONTAINER_CLASS}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        urlTransform={urlTransform}
        components={components}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
