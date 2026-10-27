import { useEffect, useRef, useState } from 'react';
import { Activity, BarChart3, BookOpen, FlaskConical, MonitorPlay, RadioTower, Settings2, SlidersHorizontal } from 'lucide-react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useDemoStore } from './demoStore';

export function AppShell() {
  const sessionId = useDemoStore((state) => state.sessionId);
  const location = useLocation();
  const navRef = useRef<HTMLElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });
  const navigation = [
    { to: '/setup', label: '开播准备', icon: RadioTower },
    { to: `/studio/${sessionId}`, label: '主播控制台', icon: SlidersHorizontal },
    { to: '/words', label: '题库管理', icon: BookOpen },
    { to: '/scoring', label: '评分实验室', icon: FlaskConical },
    { to: '/replay', label: '诊断与回放', icon: Activity },
    { to: `/analytics/${sessionId}`, label: '场次复盘', icon: BarChart3 },
    { to: '/settings', label: '设置', icon: Settings2 },
  ];

  useEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const updateOverflow = () => setOverflow({
      left: nav.scrollLeft > 1,
      right: nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 1,
    });
    updateOverflow();
    nav.addEventListener('scroll', updateOverflow, { passive: true });
    window.addEventListener('resize', updateOverflow);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updateOverflow);
    observer?.observe(nav);
    return () => {
      nav.removeEventListener('scroll', updateOverflow);
      window.removeEventListener('resize', updateOverflow);
      observer?.disconnect();
    };
  }, []);

  useEffect(() => {
    const nav = navRef.current;
    const active = nav?.querySelector<HTMLElement>('.nav-link.active');
    if (!nav || !active || nav.scrollWidth <= nav.clientWidth || typeof active.scrollIntoView !== 'function') return;
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    active.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
  }, [location.pathname]);

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><MonitorPlay size={22} /></div><div><strong>词温</strong><small>WORDHEAT LIVE</small></div></div>
      <div className="nav-label">LIVE OPERATIONS</div>
      <div className="nav-scroll" data-overflow-left={overflow.left} data-overflow-right={overflow.right}>
        <nav ref={navRef} className="nav-list" aria-label="后台页面">{navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}><Icon size={18} aria-hidden /><span>{label}</span></NavLink>)}</nav>
      </div>
      <div className="sidebar-foot"><strong>合成数据演练</strong>fixture 端口已接入<br />真实 API 待接入<br />v0.1.0 · LW-C1</div>
    </aside>
    <main className="app-main"><div className="demo-banner"><FlaskConical size={14} />全部页面使用合成 fixture；不表示真实平台、模型质量或发行验收通过</div><Outlet /></main>
  </div>;
}
