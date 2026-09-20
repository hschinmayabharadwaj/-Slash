import React, { useEffect, useRef } from 'react';

// Stage configuration
const STAGES = [
  { key: 'pending', label: 'Pending', color: '#6e7681', bgColor: '#21262d' },
  { key: 'planning', label: 'Planning', color: '#58a6ff', bgColor: '#1c2c45' },
  { key: 'awaiting_approval', label: 'Awaiting\nApproval', color: '#d29922', bgColor: '#2d2010' },
  { key: 'implementing', label: 'Implementing', color: '#f78166', bgColor: '#3d1a15' },
  { key: 'awaiting_impl_approval', label: 'Awaiting\nDiff Review', color: '#d29922', bgColor: '#2d2010' },
  { key: 'creating_pr', label: 'Creating\nPR', color: '#bc8cff', bgColor: '#2d1f45' },
  { key: 'completed', label: 'Completed', color: '#3fb950', bgColor: '#0f2d17' },
];

function Arrow({ active }) {
  return (
    <div className="pipeline-arrow-container">
      <div className="pipeline-arrow">
        <svg width="32" height="16" viewBox="0 0 32 16" fill="none">
          <path
            d="M0 8 H26 M20 2 L28 8 L20 14"
            stroke={active ? '#58a6ff' : '#30363d'}
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      {active && (
        <div className="pipeline-dot-track">
          <div className="pipeline-dot" />
        </div>
      )}
    </div>
  );
}

export default function TaskPipeline({ pipelineCounts = {}, onStageClick }) {
  return (
    <div className="pipeline-scroll-wrapper">
      <div className="pipeline-container">
        {STAGES.map((stage, i) => {
          const count = pipelineCounts[stage.key] || 0;
          const isActive = count > 0 && stage.key !== 'completed' && stage.key !== 'pending';
          const isNextActive = i < STAGES.length - 1 &&
            (pipelineCounts[STAGES[i + 1].key] || 0) > 0;

          return (
            <React.Fragment key={stage.key}>
              <button
                className={`pipeline-stage ${isActive ? 'pipeline-stage--active' : ''}`}
                style={{
                  '--stage-color': stage.color,
                  '--stage-bg': stage.bgColor,
                  borderColor: isActive ? stage.color : '#30363d',
                }}
                onClick={() => onStageClick && onStageClick(stage.key)}
                title={`Click to filter to ${stage.label} tasks`}
              >
                <div
                  className="pipeline-stage-dot"
                  style={{ background: isActive ? stage.color : '#30363d' }}
                />
                <div className="pipeline-stage-count" style={{ color: stage.color }}>
                  {count}
                </div>
                <div className="pipeline-stage-label">
                  {stage.label.split('\n').map((line, j) => (
                    <React.Fragment key={j}>
                      {line}
                      {j < stage.label.split('\n').length - 1 && <br />}
                    </React.Fragment>
                  ))}
                </div>
              </button>

              {i < STAGES.length - 1 && (
                <Arrow active={isActive || isNextActive} />
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}
