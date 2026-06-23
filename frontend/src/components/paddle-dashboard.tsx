'use client';

import type { DashboardView } from '@/components/dashboard/shared';
import { HomeDashboard } from '@/components/dashboard/home-dashboard';
import { ProcessingFlow } from '@/components/dashboard/processing-flow';

type PaddleDashboardProps = {
  view?: DashboardView;
};

export function PaddleDashboard({ view = 'home' }: PaddleDashboardProps) {
  return view === 'processing' ? <ProcessingFlow /> : <HomeDashboard />;
}
