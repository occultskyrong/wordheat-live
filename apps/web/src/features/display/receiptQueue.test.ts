import { describe, expect, it } from 'vitest';
import { ReceiptQueue } from './receiptQueue';

describe('展示回执队列', () => {
  it('preview 永不产生回执', () => {
    const queue = new ReceiptQueue('preview', '1');
    expect(() => queue.authorize(['delivery_1'])).toThrow('PREVIEW_CANNOT_FULFILL');
  });

  it('received 不释放额度，processed 才释放', () => {
    const queue = new ReceiptQueue('primary', '12');
    queue.authorize(['delivery_1']);
    queue.markReceived('delivery_1');
    expect(queue.inflight).toBe(1);
    queue.markProcessed('delivery_1');
    expect(queue.inflight).toBe(0);
  });

  it('旧代次不能重发或修改批次', () => {
    const queue = new ReceiptQueue('primary', '12');
    queue.authorize(['delivery_1']);
    queue.rotateGeneration('13');
    expect(() => queue.markProcessed('delivery_1')).toThrow('STALE_RENDER_GENERATION');
  });
});
