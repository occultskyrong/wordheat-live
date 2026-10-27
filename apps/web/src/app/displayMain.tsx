import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { applyDisplayScenario, readDisplayScenario } from '../api/fixture/scenarios';
import { DisplayBootstrap } from '../features/display/DisplayBootstrap';
import { publicDisplayFixture } from '../test/fixtures/display';

const scenario = readDisplayScenario(window.location.search);
const initialData = applyDisplayScenario(publicDisplayFixture, scenario);
const view = scenario === 'loading'
  ? <div className="async-panel" role="status" aria-busy="true">正在加载公开展示…</div>
  : <DisplayBootstrap initialData={initialData} syncError={scenario === 'contract-error' ? 'CONTRACT_VERSION_MISMATCH' : null} />;

createRoot(document.getElementById('root')!).render(<StrictMode>{view}</StrictMode>);
