import { useEffect, useMemo } from 'react';
import type { DisplayView } from '../../api/fixture/types';
import { ObsPage } from '../../pages/obs/ObsPage';
import { authorizeResume, createSyncState, reduceDisplayEvent } from './syncMachine';
import { ReceiptQueue } from './receiptQueue';
import { defaultDisplayPreferences, loadDisplayPreferences } from '../../stores/preferencesStore';

export type SyntheticDisplayEvent = { seq: string; view: DisplayView };
export type SyntheticReceiptStage = 'received' | 'processed';

type Props = {
  initialData: DisplayView;
  events?: SyntheticDisplayEvent[];
  ready?: boolean;
  onReceipt?: (stage: SyntheticReceiptStage, deliveryId: string) => void;
  syncError?: string | null;
};

export function DisplayBootstrap({ initialData, events = [], ready = true, onReceipt, syncError }: Props) {
  const purpose = initialData.purpose;
  const state = useMemo(() => {
    const snapshot = { ...initialData, purpose };
    const resumed = events.reduce(
      (current, event) => reduceDisplayEvent(current, { ...event, view: { ...event.view, purpose } }),
      createSyncState(snapshot, snapshot.asOfSeq),
    );
    return ready && !resumed.error && snapshot.authorization === 'AUTHORIZED'
      ? authorizeResume(resumed, resumed.lastSeq)
      : resumed;
  }, [initialData, events, purpose, ready]);
  const data = state.view;
  const queue = useMemo(() => new ReceiptQueue(purpose, data.renderGeneration), [purpose, data.renderGeneration]);
  const deliveryKey = data.authorizedDeliveryIds.join(',');
  const latestId = data.latest?.id;

  useEffect(() => {
    if (!state.receiptsEnabled || purpose === 'preview' || syncError) return;
    queue.authorize(data.authorizedDeliveryIds);
    for (const deliveryId of data.authorizedDeliveryIds) {
      queue.markReceived(deliveryId);
      onReceipt?.('received', deliveryId);
    }
    if (!latestId || document.visibilityState !== 'visible') return;
    const feedbackIndex = data.feedback.findIndex((feedback) => feedback.id === latestId);
    const deliveryId = data.authorizedDeliveryIds[feedbackIndex];
    if (!deliveryId) return;
    const node = document.querySelector(`[data-feedback-id="${latestId}"]`);
    if (!node?.textContent?.trim()) return;
    const frame = requestAnimationFrame(() => {
      if (document.visibilityState !== 'visible') return;
      queue.markProcessed(deliveryId);
      onReceipt?.('processed', deliveryId);
    });
    return () => cancelAnimationFrame(frame);
  }, [queue, state.receiptsEnabled, purpose, syncError, deliveryKey, latestId, data.feedback, data.authorizedDeliveryIds, onReceipt]);

  const preferences = purpose === 'preview' ? loadDisplayPreferences() : defaultDisplayPreferences;
  return <ObsPage
    initialData={data}
    syncError={syncError ?? state.error}
    layout={purpose === 'preview' ? preferences.layout : 'landscape'}
    fontScale={preferences.fontScale}
    safeArea={purpose === 'preview' && preferences.safeArea}
    reducedMotion={preferences.reducedMotion}
  />;
}
