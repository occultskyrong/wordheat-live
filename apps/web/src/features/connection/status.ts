import type { RunPurpose, TransportStatus } from '../../api/fixture/types';

export interface ConnectionInput {
  transport: TransportStatus;
  leaseValid: boolean;
  hasUnrecoverableGap: boolean;
  runPurpose: RunPurpose;
}

export function deriveConnectionStatus(input: ConnectionInput) {
  const reconnecting = input.transport === 'RECONNECTING';
  const transportFailed = input.transport === 'DISCONNECTED' || input.transport === 'FAULTED';
  const canStart = input.transport === 'CONNECTED' && input.leaseValid && !input.hasUnrecoverableGap;
  return {
    canStart,
    clockContinues: reconnecting,
    leaseCommandsBlocked: reconnecting || !input.leaseValid || input.hasUnrecoverableGap,
    isProduction: input.runPurpose === 'production',
    label: reconnecting ? '数据重连中，回合时间继续' : input.hasUnrecoverableGap ? '事件缺口不可补齐，等待恢复' : !input.leaseValid ? '租约失效，禁止开播' : transportFailed ? '连接不可用' : input.transport === 'CONNECTING' ? '连接建立中' : '连接已确认',
  };
}
