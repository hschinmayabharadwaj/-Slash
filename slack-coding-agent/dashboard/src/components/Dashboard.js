import React, { useState, useEffect } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, Legend,
} from 'recharts';
import {
  CheckCircle2, XCircle, Zap, ListTodo, TrendingUp, TrendingDown,
  Minus, Server, Database, Activity,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import TaskPipeline from './TaskPipeline';
import { fetchStats } from '../services/api';

// ─── Color maps ───────────────────────────────────────────────────────────────

const STATUS_COLORS = {
  pending: '#6e7681',
  planning: '#58a6ff',
  awaiting_approval: '#d29922',
  implementing: '#f78166',
  awaiting_impl_approval: '#d29922',
  creating_pr: '#bc8cff',
  completed: '#3fb950',
  failed: '#f85149',
};

const PIE_COLORS = ['#3fb950', '#58a6ff', '#d29922', '#f78166', '#bc8cff', '#f85149', '#6e7681'];

// ─── Stat card ────────────────────────────────────────────────────────────────

function StatCard({ icon: Icon, label, value, yesterday, color }) {
  const diff = value - (yesterday || 0);
  const TrendIcon = diff > 0 ? TrendingUp : diff < 0 ? TrendingDown : Minus;
  const trendColor = diff > 0 ? '#3fb950' : diff < 0 ? '#f85149' : '#6e7681';

  return (
    <div className="stat-card card-hover">
      <div className="stat-card-header">
        <div className="stat-icon" style={{ background: `${color}22`, color }}>
          <Icon size={20} />
        </div>
        <div className="stat-trend" style={{ color: trendColor }}>
          <TrendIcon size={14} />
          <span>{Math.abs(diff)}</span>
        </div>
      </div>
      <div className="stat-value">{value ?? '—'}</div>
      <div className="stat-label">{label}</div>
      <div className="stat-yesterday">vs {yesterday ?? 0} yesterday</div>
    </div>
  );
}

// ─── Bot status card ──────────────────────────────────────────────────────────

function StatusIndicator({ status }) {
  const color = status === 'healthy' ? '#3fb950' : '#f85149';
  return (
    <span className="infra-dot" style={{ background: color }}>
      <span className="infra-dot-pulse" style={{ background: color }} />
    </span>
  );
}

function BotStatusCard({ infraStatus }) {
  if (!infraStatus) return null;

  return (
    <div className="bot-status-card card-hover">
      <div className="card-header">
        <Activity size={16} />
        <h3>Infrastructure Status</h3>
      </div>
      <div className="infra-rows">
        <div className="infra-row">
          <div className="infra-row-left">
            <Server size={15} />
            <span>ECS Service</span>
          </div>
          <div className="infra-row-right">
            <StatusIndicator status={infraStatus.ecs.status} />
            <span className="infra-detail">{infraStatus.ecs.detail}</span>
          </div>
        </div>
        <div className="infra-row">
          <div className="infra-row-left">
            {/* cloud icon substitute */}
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" />
            </svg>
            <span>SQS Queue</span>
          </div>
          <div className="infra-row-right">
            <StatusIndicator status={infraStatus.sqs.status} />
            <span className="infra-detail">{infraStatus.sqs.detail}</span>
          </div>
        </div>
        <div className="infra-row">
          <div className="infra-row-left">
            <Database size={15} />
            <span>Aurora DB</span>
          </div>
          <div className="infra-row-right">
            <StatusIndicator status={infraStatus.aurora.status} />
            <span className="infra-detail">{infraStatus.aurora.detail}</span>
          </div>
        </div>
      </div>
      <div className="infra-last-activity">
        Last activity: {formatDistanceToNow(new Date(infraStatus.lastActivity), { addSuffix: true })}
      </div>
    </div>
  );
}

// ─── Activity feed ────────────────────────────────────────────────────────────

function ActivityFeed({ activities }) {
  if (!activities || activities.length === 0) return null;

  return (
    <div className="activity-card card-hover">
      <div className="card-header">
        <Zap size={16} />
        <h3>Recent Activity</h3>
      </div>
      <div className="activity-list">
        {activities.map((item) => (
          <div key={item.id} className="activity-item">
            <span className="activity-icon">{item.icon}</span>
            <div className="activity-content">
              <span className="activity-text">{item.text}</span>
              <span className="activity-time">
                {formatDistanceToNow(new Date(item.time), { addSuffix: true })}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Custom recharts tooltip ──────────────────────────────────────────────────

function CustomBarTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-label">{label}</div>
      <div className="chart-tooltip-value">{payload[0].value} tasks</div>
    </div>
  );
}

function CustomPieTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-label">{payload[0].name}</div>
      <div className="chart-tooltip-value">{payload[0].value} tasks</div>
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────

export default function Dashboard({ stats, onNavigateToTasks, onTaskClick }) {
  const [localStats, setLocalStats] = useState(stats);

  useEffect(() => {
    if (stats) setLocalStats(stats);
  }, [stats]);

  // Load stats locally if parent hasn't provided them yet
  useEffect(() => {
    if (!stats) {
      fetchStats().then(setLocalStats).catch(console.error);
    }
  }, [stats]);

  const s = localStats;

  function handleStageClick(stageKey) {
    onNavigateToTasks && onNavigateToTasks({ statusFilter: stageKey });
  }

  return (
    <div className="dashboard">
      {/* Stats row */}
      <div className="stats-row">
        <StatCard
          icon={ListTodo}
          label="Total Tasks"
          value={s?.total}
          yesterday={s?.totalYesterday}
          color="#58a6ff"
        />
        <StatCard
          icon={Zap}
          label="Active Now"
          value={s?.activeNow}
          yesterday={s?.activeYesterday}
          color="#d29922"
        />
        <StatCard
          icon={CheckCircle2}
          label="Completed Today"
          value={s?.completedToday}
          yesterday={s?.completedYesterday}
          color="#3fb950"
        />
        <StatCard
          icon={XCircle}
          label="Failed"
          value={s?.failed}
          yesterday={s?.failedYesterday}
          color="#f85149"
        />
      </div>

      {/* Pipeline */}
      <div className="section-card card-hover">
        <div className="card-header">
          <Zap size={16} />
          <h3>Task Pipeline</h3>
          <span className="card-header-sub">Click a stage to filter</span>
        </div>
        <TaskPipeline
          pipelineCounts={s?.pipelineCounts || {}}
          onStageClick={handleStageClick}
        />
      </div>

      {/* Charts row */}
      <div className="charts-row">
        {/* Bar chart */}
        <div className="chart-card card-hover">
          <div className="card-header">
            <h3>Tasks per Hour (last 24h)</h3>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart
              data={s?.hourlyData || []}
              margin={{ top: 5, right: 10, left: -20, bottom: 5 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis
                dataKey="hour"
                tick={{ fill: '#6e7681', fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: '#30363d' }}
                interval={3}
              />
              <YAxis
                tick={{ fill: '#6e7681', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                allowDecimals={false}
              />
              <Tooltip content={<CustomBarTooltip />} />
              <Bar dataKey="count" fill="#238636" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Pie chart */}
        <div className="chart-card card-hover">
          <div className="card-header">
            <h3>Status Distribution</h3>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <PieChart>
              <Pie
                data={s?.statusDistribution || []}
                cx="50%"
                cy="50%"
                innerRadius={50}
                outerRadius={80}
                paddingAngle={3}
                dataKey="value"
              >
                {(s?.statusDistribution || []).map((entry, index) => (
                  <Cell
                    key={entry.name}
                    fill={STATUS_COLORS[entry.name] || PIE_COLORS[index % PIE_COLORS.length]}
                  />
                ))}
              </Pie>
              <Tooltip content={<CustomPieTooltip />} />
              <Legend
                formatter={(value) => (
                  <span style={{ color: '#8b949e', fontSize: 11 }}>{value}</span>
                )}
              />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Activity + Infra status */}
      <div className="bottom-row">
        <ActivityFeed activities={s?.recentActivity} />
        <BotStatusCard infraStatus={s?.infraStatus} />
      </div>
    </div>
  );
}
