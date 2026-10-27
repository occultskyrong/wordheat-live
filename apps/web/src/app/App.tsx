import { Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom';
import { fixtureAdapter } from '../api/fixture/adapter';
import { applyDisplayScenario, readDisplayScenario } from '../api/fixture/scenarios';
import { DisplayBootstrap } from '../features/display/DisplayBootstrap';
import { AnalyticsPage } from '../pages/analytics/AnalyticsPage';
import { ReplayPage } from '../pages/replay/ReplayPage';
import { ScoringPage } from '../pages/scoring/ScoringPage';
import { SettingsPage } from '../pages/settings/SettingsPage';
import { SetupPage } from '../pages/setup/SetupPage';
import { StudioPage } from '../pages/studio/StudioPage';
import { WordsPage } from '../pages/words/WordsPage';
import { AppShell } from './AppShell';
import { FixtureQuery } from './FixtureQuery';

function StudioRoute() {
  const { sessionId = 'session_demo' } = useParams();
  return <FixtureQuery queryKey={`studio:${sessionId}`} queryFn={fixtureAdapter.getStudio}>{(data) => <StudioPage initialData={{ ...data, sessionId }} />}</FixtureQuery>;
}

function AnalyticsRoute() {
  const { sessionId = 'session_demo' } = useParams();
  return <FixtureQuery queryKey={`analytics:${sessionId}`} queryFn={fixtureAdapter.getAnalytics}>{(data, { stale }) => <AnalyticsPage initialData={data} sessionId={sessionId} stale={stale} />}</FixtureQuery>;
}

function ObsRoute() {
  const { sessionId = 'session_demo' } = useParams();
  const location = useLocation();
  const scenario = readDisplayScenario(location.search);
  const containerScenario = scenario === 'loading' || scenario === 'contract-error' ? scenario : 'normal';
  return <FixtureQuery scenario={containerScenario} queryKey={`display:${sessionId}`} queryFn={fixtureAdapter.getPublicDisplay}>{(data) => <DisplayBootstrap initialData={{ ...applyDisplayScenario(data, scenario), sessionId, purpose: 'primary' }} />}</FixtureQuery>;
}

export function AppRoutes() {
  return <Routes>
    <Route element={<AppShell />}>
      <Route path="/" element={<Navigate to="/setup" replace />} />
      <Route path="/setup" element={<FixtureQuery queryKey="setup" queryFn={fixtureAdapter.getSetup}>{(data) => <SetupPage initialData={data} />}</FixtureQuery>} />
      <Route path="/studio/:sessionId" element={<StudioRoute />} />
      <Route path="/words" element={<FixtureQuery queryKey="words" queryFn={fixtureAdapter.getWords}>{(data, { stale }) => <WordsPage initialData={data} stale={stale} />}</FixtureQuery>} />
      <Route path="/scoring" element={<FixtureQuery queryKey="scoring" queryFn={fixtureAdapter.getScoring}>{(data) => <ScoringPage initialData={data} />}</FixtureQuery>} />
      <Route path="/replay" element={<FixtureQuery queryKey="diagnostics" queryFn={fixtureAdapter.getDiagnostics}>{(data) => <ReplayPage initialData={data} />}</FixtureQuery>} />
      <Route path="/analytics/:sessionId" element={<AnalyticsRoute />} />
      <Route path="/settings" element={<FixtureQuery queryKey="settings" queryFn={fixtureAdapter.getSettings}>{(data) => <SettingsPage initialData={data} />}</FixtureQuery>} />
    </Route>
    <Route path="/obs/:sessionId" element={<ObsRoute />} />
    <Route path="*" element={<Navigate to="/setup" replace />} />
  </Routes>;
}
