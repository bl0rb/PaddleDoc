import type { Metadata } from "next";
import { Lora, Source_Sans_3 } from 'next/font/google';
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
  title: "PaddleDock",
  description: "Document processing pipeline powered by PaddleOCR",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`h-full antialiased ${sourceSans.variable} ${lora.variable}`}>
      <body className="min-h-full flex flex-col">
        <SidebarNav />
        {children}
      </body>
    </html>
  );
}
