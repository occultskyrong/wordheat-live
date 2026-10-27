type ReceiptStage = 'authorized' | 'received' | 'processed';

export class ReceiptQueue {
  private items = new Map<string, { generation: string; stage: ReceiptStage }>();

  constructor(private purpose: 'primary' | 'preview', private generation: string) {}

  get inflight(): number {
    return [...this.items.values()].filter((item) => item.generation === this.generation && item.stage !== 'processed').length;
  }

  authorize(deliveryIds: string[]): void {
    if (this.purpose === 'preview') throw new Error('PREVIEW_CANNOT_FULFILL');
    if (this.inflight + deliveryIds.length > 128) throw new Error('DISPLAY_CREDIT_EXCEEDED');
    for (const id of deliveryIds) this.items.set(id, { generation: this.generation, stage: 'authorized' });
  }

  markReceived(deliveryId: string): void {
    this.transition(deliveryId, 'received');
  }

  markProcessed(deliveryId: string): void {
    this.transition(deliveryId, 'processed');
  }

  rotateGeneration(generation: string): void {
    this.generation = generation;
  }

  private transition(deliveryId: string, stage: ReceiptStage): void {
    const item = this.items.get(deliveryId);
    if (!item || item.generation !== this.generation) throw new Error('STALE_RENDER_GENERATION');
    item.stage = stage;
  }
}
