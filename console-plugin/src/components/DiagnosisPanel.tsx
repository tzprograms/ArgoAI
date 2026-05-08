import * as React from 'react';
import {
  Panel,
  PanelMain,
  PanelMainBody,
  PanelHeader,
  Spinner,
  Label,
  Title,
  Split,
  SplitItem,
  Switch,
} from '@patternfly/react-core';
import type { DiagnosisEvent, DiagnosisResult } from '../utils/api';
import DiagnosisResultView from './DiagnosisResult';

interface DiagnosisPanelProps {
  events: DiagnosisEvent[];
  isRunning: boolean;
  result: DiagnosisResult | null;
  agentName: string;
  error: string | null;
}

const EventLine: React.FC<{ event: DiagnosisEvent }> = ({ event }) => {
  const timestamp = new Date().toLocaleTimeString();

  switch (event.type) {
    case 'triage_start':
      return (
        <div className="argoai-event argoai-event--triage">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="blue" isCompact>triage</Label>{' '}
          {event.content}
        </div>
      );
    case 'routing':
      return (
        <div className="argoai-event argoai-event--routing">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="purple" isCompact>routing</Label>{' '}
          Selected <strong>{event.agent_name}</strong> — {event.reason}
        </div>
      );
    case 'start':
      return (
        <div className="argoai-event argoai-event--start">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="blue" isCompact>start</Label>{' '}
          {event.content}
        </div>
      );
    case 'tool_call':
      return (
        <div className="argoai-event argoai-event--tool">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="teal" isCompact>tool_call</Label>{' '}
          Calling <code>{event.tool}</code>
        </div>
      );
    case 'tool_result':
      return (
        <div className="argoai-event argoai-event--result">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="teal" isCompact>tool_result</Label>
          <pre className="argoai-event__pre">
            {event.content?.substring(0, 300)}
            {(event.content?.length || 0) > 300 ? '...' : ''}
          </pre>
        </div>
      );
    case 'reasoning':
      return (
        <div className="argoai-event argoai-event--reasoning">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="yellow" isCompact>reasoning</Label>{' '}
          {event.content}
        </div>
      );
    case 'warning':
      return (
        <div className="argoai-event argoai-event--warning">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="orange" isCompact>warning</Label>{' '}
          {event.content}
        </div>
      );
    case 'cache_hit':
      return (
        <div className="argoai-event argoai-event--cache">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="green" isCompact>cache_hit</Label>{' '}
          {event.content}
        </div>
      );
    case 'usage':
      return (
        <div className="argoai-event argoai-event--usage">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="grey" isCompact>usage</Label>{' '}
          {event.content}
        </div>
      );
    case 'error':
      return (
        <div className="argoai-event argoai-event--error">
          <span className="argoai-event__time">{timestamp}</span>
          <Label color="red" isCompact>error</Label>{' '}
          {event.error || event.content}
        </div>
      );
    default:
      return null;
  }
};

const DiagnosisPanel: React.FC<DiagnosisPanelProps> = ({
  events,
  isRunning,
  result,
  agentName,
  error,
}) => {
  const logEndRef = React.useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = React.useState(true);

  React.useEffect(() => {
    if (autoScroll && logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [events, autoScroll]);

  return (
    <div className="argoai-diagnosis-panel">
      {/* Live Agent Log */}
      <Panel isScrollable>
        <PanelHeader>
          <Split hasGutter>
            <SplitItem isFilled>
              <Title headingLevel="h4">
                Agent Logs{' '}
                {isRunning && (
                  <Label color="green" isCompact>
                    Live
                  </Label>
                )}
              </Title>
            </SplitItem>
            <SplitItem>
              <Switch
                id="auto-scroll"
                label="Auto-scroll"
                isChecked={autoScroll}
                onChange={(_e, checked) => setAutoScroll(checked)}
                isReversed
              />
            </SplitItem>
          </Split>
        </PanelHeader>
        <PanelMain maxHeight="400px">
          <PanelMainBody>
            <div className="argoai-log-stream">
              {events
                .filter((e) => e.type !== 'diagnosis' && e.type !== 'done')
                .map((event, i) => (
                  <EventLine key={i} event={event} />
                ))}
              {isRunning && (
                <div className="argoai-event argoai-event--loading">
                  <Spinner size="sm" /> Processing...
                </div>
              )}
              <div ref={logEndRef} />
            </div>
          </PanelMainBody>
        </PanelMain>
      </Panel>

      {/* Diagnosis Result */}
      {result && (
        <div style={{ marginTop: '1rem' }}>
          <DiagnosisResultView result={result} agentName={agentName} />
        </div>
      )}

      {/* Error */}
      {error && !result && !isRunning && (
        <div style={{ marginTop: '1rem', color: 'var(--pf-t--global--color--status--danger--default)' }}>
          <strong>Diagnosis failed:</strong> {error}
        </div>
      )}
    </div>
  );
};

export default DiagnosisPanel;
