import { useState } from 'react';
import { Archive, Monitor, Save, ShieldCheck } from 'lucide-react';
import { fixtureAdapter } from '../../api/fixture/adapter';
import type { DemoResult, SettingsView } from '../../api/fixture/types';
import { ConfirmDialog, FieldError, InlineNotice, PageHeader, Panel, StatusPill } from '../../components/ui';
import { defaultDisplayPreferences, loadDisplayPreferences, saveDisplayPreferences, type DisplayPreferences } from '../../stores/preferencesStore';

type Props = {
  initialData: SettingsView;
  onCommand?: (command: string) => Promise<DemoResult>;
};

export function SettingsPage({ initialData, onCommand = fixtureAdapter.executeDemoCommand }: Props) {
  const [duration, setDuration] = useState(initialData.rules.duration);
  const [intermission, setIntermission] = useState(initialData.rules.intermission);
  const [autoNext, setAutoNext] = useState(initialData.rules.autoNext);
  const [nextDuration, setNextDuration] = useState(initialData.rules.duration);
  const [confirmRules, setConfirmRules] = useState(false);
  const [maintenanceConfirm, setMaintenanceConfirm] = useState<'revoke' | 'backup' | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [maintenanceMessages, setMaintenanceMessages] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [preferenceMessage, setPreferenceMessage] = useState('');
  const [preferences, setPreferences] = useState<DisplayPreferences>(loadDisplayPreferences);

  async function requestMaintenance(kind: 'revoke' | 'backup') {
    if (pending || (kind === 'backup' && initialData.activeSession)) return;
    setPending(true);
    setMessage('');
    try {
      const result = await fixtureAdapter.requestRealCapability();
      setMaintenanceMessages((current) => [...current, result.message]);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '维护请求失败，请重试');
    } finally {
      setPending(false);
      setMaintenanceConfirm(null);
    }
  }

  function updatePreferences(patch: Partial<DisplayPreferences>) {
    setPreferences((current) => {
      const next = { ...current, ...patch };
      saveDisplayPreferences(next);
      return next;
    });
  }

  async function saveRules() {
    setPending(true);
    setError('');
    try {
      await onCommand('session.configure.next_round');
      setNextDuration(duration);
      setMessage('下一轮规则已保存 · 合成演练');
      setConfirmRules(false);
    } catch (reason) {
      setConfirmRules(false);
      setError(reason instanceof Error && reason.message === 'VERSION_CONFLICT' ? '版本冲突，草稿已保留，请重新读取后再保存' : '保存失败，草稿已保留');
    } finally {
      setPending(false);
    }
  }

  return <div className="page">
    <PageHeader eyebrow="08 / PREFERENCES & MAINTENANCE" title="设置与展示预览" description="本机预览偏好、下一轮业务规则与原生维护入口彼此分离；配置变化不会暗中修改当前轮。" />
    <div className="grid two">
      <Panel title="本机预览偏好" eyebrow="LOCAL ONLY" action={<Monitor size={20} className="teal" />}>
        <div className="form-grid">
          <label className="field"><span>展示字号</span><select value={preferences.fontScale} onChange={(event) => updatePreferences({ fontScale: Number(event.target.value) as DisplayPreferences['fontScale'] })}><option value={0.9}>紧凑</option><option value={1}>标准</option><option value={1.1}>大</option><option value={1.2}>特大</option></select></label>
          <label className="field"><span>布局</span><select value={preferences.layout} onChange={(event) => updatePreferences({ layout: event.target.value as DisplayPreferences['layout'] })}><option value="portrait">竖屏 1080 × 1920</option><option value="landscape">横屏 1920 × 1080</option></select></label>
        </div>
        <label className="checkbox-row"><input type="checkbox" checked={preferences.safeArea} onChange={(event) => updatePreferences({ safeArea: event.target.checked })} />显示 OBS 安全区</label>
        <label className="checkbox-row"><input type="checkbox" checked={preferences.reducedMotion} onChange={(event) => updatePreferences({ reducedMotion: event.target.checked })} />减少预览动画</label>
        <div className="button-row mt"><button className="button" onClick={() => { saveDisplayPreferences(preferences); setPreferenceMessage('本机预览偏好已保存'); }}><Save size={16} />保存本机预览偏好</button><button className="button ghost" onClick={() => { updatePreferences(defaultDisplayPreferences); setPreferenceMessage('已恢复并保存设计默认'); }}>恢复设计默认</button></div>
        <p className="caption mt">变更立即写入本机版本化偏好，不会修改后端全局配置。主题固定为墨蓝、青绿与琥珀警示色。</p>
        {preferenceMessage && <InlineNotice>{preferenceMessage}</InlineNotice>}
      </Panel>

      <Panel title="当前身份与展示票据" eyebrow="READ ONLY IDENTITY" action={<ShieldCheck size={20} className="teal" />}>
        <div className="data-list">
          <div className="data-row"><span>当前角色</span><strong>主播 · native 初始化</strong></div>
          <div className="data-row"><span>主展示连接</span><StatusPill tone="good">preview 合成连接</StatusPill></div>
          <div className="data-row"><span>render generation</span><strong>{initialData.display.generation}</strong></div>
          <div className="data-row"><span>票据到期</span><strong>{initialData.display.expiresAt}</strong></div>
        </div>
        <div className="button-row mt"><button className="button secondary" disabled={pending} onClick={() => setMessage('复制一次性预览链接：待真实 API 接入')}>复制一次性预览链接：待真实 API 接入</button><button className="button danger" disabled={pending} onClick={() => { setMessage(''); setMaintenanceConfirm('revoke'); }}>撤销票据</button></div>
        <p className="caption mt">preview 不发送履约；完整票据不会出现在提示或日志中。</p>
      </Panel>
    </div>

    <div className="grid two mt">
      <Panel title="下一轮 SessionRules" eyebrow="APPLY: NEXT_ROUND">
        <InlineNotice>当前冻结规则：{initialData.rules.duration} 秒；保存后也只影响下一轮。</InlineNotice>
        <div className="form-grid mt">
          <label className="field"><span>回合时长（秒）</span><input type="number" min={30} max={600} value={duration} onChange={(event) => setDuration(Number(event.target.value))} /></label>
          <label className="field"><span>轮间间歇（秒）</span><input type="number" min={3} max={30} value={intermission} onChange={(event) => setIntermission(Number(event.target.value))} /></label>
          <label className="field"><span>难度下限</span><input type="number" min={1} max={5} value={initialData.rules.difficulty[0]} readOnly /></label>
          <label className="field"><span>难度上限</span><input type="number" min={1} max={5} value={initialData.rules.difficulty[1]} readOnly /></label>
        </div>
        <label className="checkbox-row"><input type="checkbox" checked={autoNext} onChange={(event) => setAutoNext(event.target.checked)} />自动续局</label>
        <label className="checkbox-row"><input type="checkbox" checked={initialData.rules.strictCode} readOnly />严格回合短码（official 必须开启）</label>
        <p className="caption">当前冻结规则：{initialData.rules.duration} 秒<br />下一轮规则：{nextDuration} 秒<br />间歇 {intermission} 秒 · 自动续局 {autoNext ? '开' : '关'}</p>
        <button className="button" disabled={pending || duration < 30 || duration > 600 || intermission < 3 || intermission > 30} onClick={() => { setMessage(''); setError(''); setConfirmRules(true); }}>保存为下一轮规则</button>
        {message && <InlineNotice>{message}</InlineNotice>}
        {error && <FieldError>{error}</FieldError>}
      </Panel>

      <Panel title="运行维护" eyebrow="NATIVE GUIDED OPERATIONS" action={<Archive size={20} className="teal" />}>
        <div className="data-list">
          <div className="data-row"><span>模型</span><strong>{initialData.model.id}<small>{initialData.model.revision} · {initialData.model.calibration}</small></strong></div>
          <div className="data-row"><span>模型状态</span><strong>{initialData.model.status}</strong></div>
          <div className="data-row"><span>最近备份</span><strong>{initialData.backup.lastSuccess}<small>{initialData.backup.version}</small></strong></div>
        </div>
        <InlineNotice tone="warn">{initialData.backup.status}。维护必须先结束活动场，并由 native 向导确认。</InlineNotice>
        {initialData.activeSession && <FieldError>活动场 {initialData.activeSession.id} · {initialData.activeSession.status}，请先结束活动场</FieldError>}
        <div className="button-row mt"><button className="button secondary" disabled={pending || initialData.activeSession !== null} onClick={() => { setMessage(''); setMaintenanceConfirm('backup'); }}>请求备份向导</button><button className="button ghost" disabled={pending} onClick={() => setMessage('维护说明：请使用随包 native 手册；Web 不执行路径、下载或迁移命令')}>维护说明</button></div>
        {maintenanceMessages.map((maintenanceMessage, index) => <InlineNotice key={`${maintenanceMessage}-${index}`}>{maintenanceMessage}</InlineNotice>)}
      </Panel>
    </div>
    {confirmRules && <ConfirmDialog title="保存下一轮规则" description={`当前轮继续使用 ${initialData.rules.duration} 秒；下一轮改为 ${duration} 秒、间歇 ${intermission} 秒。提交附带 expected_version，冲突时保留草稿。`} confirmLabel="确认保存下一轮规则" pending={pending} onCancel={() => setConfirmRules(false)} onConfirm={saveRules} />}
    {maintenanceConfirm === 'revoke' && <ConfirmDialog title="撤销展示票据" description="撤销后现有展示连接将失效；此操作只请求真实维护能力，不会伪报撤票完成。" confirmLabel="确认撤销票据" pending={pending} onCancel={() => setMaintenanceConfirm(null)} onConfirm={() => requestMaintenance('revoke')} />}
    {maintenanceConfirm === 'backup' && <ConfirmDialog title="请求备份向导" description="备份前确认当前没有活动场。确认后只请求 native 维护能力，不在 Web 中执行备份。" confirmLabel="确认请求备份向导" pending={pending} onCancel={() => setMaintenanceConfirm(null)} onConfirm={() => requestMaintenance('backup')} />}
  </div>;
}
