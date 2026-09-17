import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Search Gateway · 个人搜索控制台',
  description: '个人多搜索源管理与自动切换控制台',
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
