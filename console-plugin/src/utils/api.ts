export interface DiagnosisEvent {
  type:
    | 'triage_start'
    | 'routing'
    | 'start'
    | 'tool_call'
    | 'tool_result'
    | 'reasoning'
    | 'warning'
    | 'cache_hit'
    | 'usage'
    | 'diagnosis'
    | 'error'
    | 'done';
  content?: string;
  agent?: string;
  agent_name?: string;
  reason?: string;
  tool?: string;
  result?: DiagnosisResult;
  error?: string;
  llm_rounds?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  estimated_input_tokens?: number;
  tool_calls?: number;
  blocked_tool_calls?: number;
  truncated_tool_responses?: number;
}

export interface DiagnosisResult {
  error: string;
  cause: string;
  fix: string;
  agent?: string;
  confidence?: string;
  evidence?: string;
}

export interface DiagnoseRequest {
  appName: string;
  appNamespace: string;
  provider: string;
  apiKey?: string;
  model?: string;
}

const GO_SERVICE_URL =
  (window as any).__ARGOAI_API_URL__ || 'http://localhost:8080';

export function startDiagnosis(
  request: DiagnoseRequest,
  onEvent: (event: DiagnosisEvent) => void,
  onError: (error: string) => void,
  onComplete: () => void,
): AbortController {
  const controller = new AbortController();

  fetch(`${GO_SERVICE_URL}/api/v1/diagnose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        const text = await response.text();
        onError(text || `HTTP ${response.status}`);
        onComplete();
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        onError('No response stream');
        onComplete();
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';

      let isReading = true;
      while (isReading) {
        const { done, value } = await reader.read();
        if (done) {
          isReading = false;
          continue;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith('data:')) continue;

          const jsonStr = trimmed.slice(5).trim();
          if (!jsonStr) continue;

          try {
            const event: DiagnosisEvent = JSON.parse(jsonStr);
            onEvent(event);
            if (event.type === 'done' || event.type === 'error') {
              onComplete();
              return;
            }
          } catch {
            // skip malformed SSE lines
          }
        }
      }

      onComplete();
    })
    .catch((err) => {
      if (err.name !== 'AbortError') {
        onError(err.message || 'Network error');
      }
      onComplete();
    });

  return controller;
}

export async function checkHealth(): Promise<boolean> {
  try {
    const resp = await fetch(`${GO_SERVICE_URL}/api/v1/health`);
    return resp.ok;
  } catch {
    return false;
  }
}
