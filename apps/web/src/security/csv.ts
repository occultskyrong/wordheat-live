export type CsvValue = string | number | null | undefined;
export type CsvRow = Record<string, CsvValue>;

function protectText(value: string): string {
  return /^[\t\r\n ]*[=+\-@]/.test(value) ? `'${value}` : value;
}

function encodeCell(value: CsvValue): string {
  if (value === null || value === undefined) return '""';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('CSV 数字必须为有限值');
    return `"${String(value)}"`;
  }
  return `"${protectText(value).replaceAll('"', '""')}"`;
}

export function serializeCsv(rows: CsvRow[], columns: string[]): string {
  const header = columns.join(',');
  const body = rows.map((row) => columns.map((column) => encodeCell(row[column])).join(','));
  return [header, ...body].join('\n');
}

export function safeCsvFilename(sessionId: string, date: string): string {
  const safeSession = sessionId.replaceAll(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80) || 'session';
  const safeDate = date.replaceAll(/[^0-9-]/g, '').slice(0, 10) || 'undated';
  return `wordheat-${safeSession}-${safeDate}.csv`;
}
