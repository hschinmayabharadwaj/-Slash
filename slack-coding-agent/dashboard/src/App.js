import React, { useState, useEffect, useCallback } from 'react';
import { Amplify } from 'aws-amplify';
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
import '@aws-amplify/ui-react/styles.css';
import './App.css';
import Dashboard from './components/Dashboard';
import TaskList from './components/TaskList';
import TaskDetail from './components/TaskDetail';
import SubmitTask from './components/SubmitTask';
import Audit from './components/Audit';
import { fetchStats } from './services/api';
import {
  LayoutDashboard,
  ListTodo,
  Bot,
  LogOut,
  Wifi,
  WifiOff,
  RefreshCw,
  Menu,
  X,
  Code2,
  PlusCircle,
  ScrollText,
} from 'lucide-react';

// Configure Amplify — falls back to dummy values so the app loads without real Cognito
const cognitoRegion = process.env.REACT_APP_COGNITO_REGION || 'us-east-1';
const userPoolId = process.env.REACT_APP_COGNITO_POOL_ID || 'us-east-1_PLACEHOLDER';
const userPoolClientId = process.env.REACT_APP_COGNITO_CLIENT_ID || 'PLACEHOLDER_CLIENT_ID';

if (process.env.REACT_APP_COGNITO_POOL_ID) {
  Amplify.configure({
    Auth: {
      Cognito: {
        region: cognitoRegion,
        userPoolId,
        userPoolClientId,
      },
    },
  });
}

const USE_AUTH = Boolean(process.env.REACT_APP_COGNITO_POOL_ID);

// ─── Navigation link ────────────────────────────────────────────────────────

function NavLink({ icon: Icon, label, active, onClick }) {
  return (
    <button
      className={`nav-link ${active ? 'nav-link--active' : ''}`}
      onClick={onClick}
    >
      <Icon size={18} />
      <span>{label}</span>
    </button>
  );
}

// ─── Inner app (receives auth user or a mock user) ───────────────────────────

function AppShell({ user, signOut }) {
  const [page, setPage] = useState('dashboard'); // 'dashboard' | 'submit' | 'tasks' | 'task' | 'audit'
  const [selectedTaskId, setSelectedTaskId] = useState(null);
  const [botOnline, setBotOnline] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(new Date());
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [stats, setStats] = useState(null);

  const username =
    user?.username ||
    user?.signInDetails?.loginId ||
    user?.attributes?.email ||
    'demo@example.com';

  const refresh = useCallback(async () => {
    try {
      const s = await fetchStats();
      setStats(s);
      setBotOnline(true);
    } catch {
      setBotOnline(false);
    }
    setLastRefresh(new Date());
  }, []);

  // Initial load + auto-refresh every 10 s
  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10_000);
    return () => clearInterval(id);
  }, [refresh]);

  function navigate(p, taskId) {
    setPage(p);
    if (taskId !== undefined) setSelectedTaskId(taskId);
    setSidebarOpen(false);
  }

  function handleTaskClick(id) {
    navigate('task', id);
  }

  return (
    <div className="app-layout">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="sidebar-overlay"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside className={`sidebar ${sidebarOpen ? 'sidebar--open' : ''}`}>
        <div className="sidebar-logo">
          <Code2 size={24} color="#238636" />
          <span>CodingBot</span>
        </div>

        <nav className="sidebar-nav">
          <NavLink
            icon={LayoutDashboard}
            label="Dashboard"
            active={page === 'dashboard'}
            onClick={() => navigate('dashboard')}
          />
          <NavLink
            icon={PlusCircle}
            label="New Task"
            active={page === 'submit'}
            onClick={() => navigate('submit')}
          />
          <NavLink
            icon={ListTodo}
            label="All Tasks"
            active={page === 'tasks' || page === 'task'}
            onClick={() => navigate('tasks')}
          />
          <NavLink
            icon={ScrollText}
            label="Audit"
            active={page === 'audit'}
            onClick={() => navigate('audit')}
          />
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="avatar">
              {(username[0] || 'U').toUpperCase()}
            </div>
            <span className="sidebar-username">{username}</span>
          </div>
          {signOut && (
            <button className="btn-icon" onClick={signOut} title="Sign out">
              <LogOut size={16} />
            </button>
          )}
        </div>
      </aside>

      {/* Main area */}
      <div className="main-wrapper">
        {/* Header */}
        <header className="topbar">
          <button
            className="btn-icon mobile-menu-btn"
            onClick={() => setSidebarOpen((o) => !o)}
          >
            {sidebarOpen ? <X size={20} /> : <Menu size={20} />}
          </button>

          <div className="topbar-title">
            {page === 'dashboard' && 'Live Dashboard'}
            {page === 'submit' && 'Submit a Task'}
            {page === 'tasks' && 'Task History'}
            {page === 'task' && `Task #${selectedTaskId}`}
            {page === 'audit' && 'Audit Trail'}
          </div>

          <div className="topbar-actions">
            <div className={`bot-status ${botOnline ? 'bot-status--online' : 'bot-status--offline'}`}>
              {botOnline ? <Wifi size={14} /> : <WifiOff size={14} />}
              <span>{botOnline ? 'Bot Online' : 'Bot Offline'}</span>
            </div>

            <button
              className="btn-secondary"
              onClick={refresh}
              title="Refresh now"
            >
              <RefreshCw size={14} />
              <span className="hide-mobile">Refresh</span>
            </button>

            <span className="last-refresh">
              {lastRefresh.toLocaleTimeString()}
            </span>
          </div>
        </header>

        {/* Page content */}
        <main className="page-content">
          {page === 'dashboard' && (
            <Dashboard
              stats={stats}
              onNavigateToTasks={() => navigate('tasks')}
              onTaskClick={handleTaskClick}
            />
          )}
          {page === 'submit' && (
            <SubmitTask onTaskCreated={(taskId) => navigate('task', taskId)} />
          )}
          {page === 'tasks' && (
            <TaskList onTaskClick={handleTaskClick} />
          )}
          {page === 'task' && (
            <TaskDetail
              taskId={selectedTaskId}
              onBack={() => navigate('tasks')}
            />
          )}
          {page === 'audit' && (
            <Audit />
          )}
        </main>
      </div>
    </div>
  );
}

// ─── Root export ─────────────────────────────────────────────────────────────

export default function App() {
  if (!USE_AUTH) {
    // Dev / demo mode — skip Cognito entirely
    return (
      <AppShell
        user={{ username: 'demo@example.com' }}
        signOut={null}
      />
    );
  }

  return (
    <Authenticator
      loginMechanisms={['email']}
      variation="modal"
      components={{
        Header() {
          return (
            <div className="auth-header">
              <Code2 size={32} color="#238636" />
              <h2>Slack Coding Agent</h2>
              <p>Sign in to access the live dashboard</p>
            </div>
          );
        },
      }}
    >
      {({ signOut, user }) => (
        <AppShell user={user} signOut={signOut} />
      )}
    </Authenticator>
  );
}
