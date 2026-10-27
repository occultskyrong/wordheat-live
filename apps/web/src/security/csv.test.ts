import { describe, expect, it } from 'vitest';
import { safeCsvFilename, serializeCsv } from './csv';

describe('安全 CSV', () => {
  it('转义引号、逗号和换行并生成 UTF-8 文本', () => {
    const csv = serializeCsv([{ word: '苹果, "青"', note: '第一行\n第二行' }], ['word', 'note']);
    expect(csv).toBe('word,note\n"苹果, ""青""","第一行\n第二行"');
  });

  it.each(['=1+1', '+SUM(A1:A2)', '-2+3', '@cmd', '\t=1+1', '\r@cmd'])('将危险公式 %s 作为文本保护', (value) => {
    const csv = serializeCsv([{ value }], ['value']);
    expect(csv.split('\n')[1]).toBe(`"'${value}"`);
  });

  it('拒绝非有限数字并清理文件名', () => {
    expect(() => serializeCsv([{ value: Number.POSITIVE_INFINITY }], ['value'])).toThrow('CSV 数字必须为有限值');
    expect(safeCsvFilename('session/demo:01', '2026-10-25')).toBe('wordheat-session_demo_01-2026-10-25.csv');
  });
});
