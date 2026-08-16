/**
 * The PaddleDoc mark: two crossed paddles behind a document page, on the
 * emerald gradient tile. Single source of truth for the in-app brand —
 * the favicon/app icons (src/app/icon.svg, apple-icon.png, favicon.ico)
 * are exports of this same artwork.
 */
export function PaddleDocLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true">
      <defs>
        <linearGradient id="pdlogo-bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#10b981" />
          <stop offset="1" stopColor="#047857" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="14" fill="url(#pdlogo-bg)" />
      <g transform="rotate(32 32 32)">
        <rect x="29.9" y="6" width="4.2" height="34" rx="2.1" fill="#065f46" />
        <path
          d="M32 38 c5.6 0 8 4.4 8 9.3 c0 5.9 -3.7 10 -8 10 c-4.3 0 -8 -4.1 -8 -10 c0 -4.9 2.4 -9.3 8 -9.3 Z"
          fill="#065f46"
        />
        <rect x="28.4" y="3.4" width="7.2" height="3.8" rx="1.9" fill="#065f46" />
      </g>
      <g transform="rotate(-32 32 32)">
        <rect x="29.9" y="6" width="4.2" height="34" rx="2.1" fill="#a7f3d0" />
        <path
          d="M32 38 c5.6 0 8 4.4 8 9.3 c0 5.9 -3.7 10 -8 10 c-4.3 0 -8 -4.1 -8 -10 c0 -4.9 2.4 -9.3 8 -9.3 Z"
          fill="#a7f3d0"
        />
        <rect x="28.4" y="3.4" width="7.2" height="3.8" rx="1.9" fill="#a7f3d0" />
      </g>
      <path d="M21 17 a3 3 0 0 1 3 -3 h11 l8 8 v20 a3 3 0 0 1 -3 3 H24 a3 3 0 0 1 -3 -3 Z" fill="#ffffff" />
      <path d="M35 14 l8 8 h-6 a2 2 0 0 1 -2 -2 Z" fill="#a7f3d0" />
      <rect x="25" y="24" width="9" height="2.6" rx="1.3" fill="#059669" />
      <rect x="25" y="29.5" width="13" height="2.6" rx="1.3" fill="#059669" />
      <rect x="25" y="35" width="10" height="2.6" rx="1.3" fill="#059669" />
    </svg>
  );
}
