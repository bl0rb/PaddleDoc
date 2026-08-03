import type { Metadata } from "next";
import { Lora, Source_Sans_3 } from 'next/font/google';
import Script from 'next/script';
import { SidebarNav } from '@/components/sidebar-nav';
import "./globals.css";

const sourceSans = Source_Sans_3({
  subsets: ['latin'],
  variable: '--font-sans',
});

const lora = Lora({
  subsets: ['latin'],
  variable: '--font-serif',
});

export const metadata: Metadata = {
  title: "PaddleDoc",
  description: "Document processing pipeline powered by PaddleOCR",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`h-full antialiased ${sourceSans.variable} ${lora.variable}`}>
      <head>
        {/*
          `beforeInteractive` is Next.js's blessed way to run a script
          synchronously, before hydration/any client bundle executes —
          it's injected into the initial HTML and executed first, same
          effect as a plain synchronous <script> in <head> but without
          tripping the no-sync-scripts lint rule. Served by
          src/app/runtime-env.js/route.ts, which reads process.env live
          on every request under `next start` — this is what makes the
          backend API URL configurable at container start instead of
          being baked into the build.
        */}
        <Script src="/runtime-env.js" strategy="beforeInteractive" />
      </head>
      <body className="min-h-full flex flex-col">
        <SidebarNav />
        {children}
      </body>
    </html>
  );
}
