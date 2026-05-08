import * as React from 'react';
import {
  Page,
  PageSection,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
  Button,
  FormGroup,
  TextInput,
  FormSelect,
  FormSelectOption,
  Modal,
  ModalVariant,
  ModalHeader,
  ModalBody,
  EmptyState,
  EmptyStateBody,
  Spinner,
  Label,
  Alert,
  Split,
  SplitItem,
} from '@patternfly/react-core';
import {
  Table,
  Thead,
  Tr,
  Th,
  Tbody,
  Td,
} from '@patternfly/react-table';
import { useK8sWatchResource } from '@openshift-console/dynamic-plugin-sdk';
import type {
  DiagnosisEvent,
  DiagnosisResult,
  DiagnoseRequest,
} from '../utils/api';
import { startDiagnosis } from '../utils/api';
import DiagnosisPanel from './DiagnosisPanel';
import './ArgoAgentPage.scss';

interface ArgoApp {
  metadata: {
    name: string;
    namespace: string;
  };
  status?: {
    health?: { status?: string };
    sync?: { status?: string };
    conditions?: Array<{ type?: string; message?: string }>;
  };
  spec?: {
    destination?: { namespace?: string };
  };
}

const PROVIDERS = [
  { value: 'gemini', label: 'Gemini (Google)' },
  { value: 'openai', label: 'GPT-4o-mini (OpenAI)' },
  { value: 'anthropic', label: 'Claude 3 Haiku (Anthropic)' },
  { value: 'groq', label: 'Llama 3.1 8B (Groq)' },
  { value: 'openrouter', label: 'GPT OSS 20B (OpenRouter)' },
  { value: 'ollama', label: 'Local Model (Ollama)' },
];

const healthColor = (status?: string): 'green' | 'red' | 'orange' | 'grey' => {
  switch (status) {
    case 'Healthy':
      return 'green';
    case 'Degraded':
    case 'Missing':
      return 'red';
    case 'Progressing':
      return 'orange';
    default:
      return 'grey';
  }
};

const syncColor = (status?: string): 'green' | 'orange' | 'grey' => {
  switch (status) {
    case 'Synced':
      return 'green';
    case 'OutOfSync':
      return 'orange';
    default:
      return 'grey';
  }
};

const ArgoAgentPage: React.FC = () => {
  const [apps, appsLoaded, appsError] = useK8sWatchResource<ArgoApp[]>({
    groupVersionKind: {
      group: 'argoproj.io',
      version: 'v1alpha1',
      kind: 'Application',
    },
    isList: true,
    namespaced: true,
  });

  // Diagnosis state
  const [selectedApp, setSelectedApp] = React.useState<ArgoApp | null>(null);
  const [isModalOpen, setIsModalOpen] = React.useState(false);
  const [isDiagnosing, setIsDiagnosing] = React.useState(false);
  const [diagEvents, setDiagEvents] = React.useState<DiagnosisEvent[]>([]);
  const [diagResult, setDiagResult] = React.useState<DiagnosisResult | null>(null);
  const [diagError, setDiagError] = React.useState<string | null>(null);
  const [agentName, setAgentName] = React.useState('');
  const abortRef = React.useRef<AbortController | null>(null);

  // Provider config
  const [provider, setProvider] = React.useState('gemini');
  const [apiKey, setApiKey] = React.useState('');

  const handleDiagnose = (app: ArgoApp) => {
    setSelectedApp(app);
    setIsModalOpen(true);
    setDiagEvents([]);
    setDiagResult(null);
    setDiagError(null);
    setAgentName('');
    setIsDiagnosing(true);

    const request: DiagnoseRequest = {
      appName: app.metadata.name,
      appNamespace: app.metadata.namespace,
      provider,
      apiKey: provider === 'ollama' ? undefined : apiKey,
    };

    abortRef.current = startDiagnosis(
      request,
      (event) => {
        setDiagEvents((prev) => [...prev, event]);
        if (event.type === 'routing' && event.agent_name) {
          setAgentName(event.agent_name);
        }
        if (event.type === 'diagnosis' && event.result) {
          setDiagResult(event.result);
        }
        if (event.type === 'error') {
          setDiagError(event.error || event.content || 'Unknown error');
        }
      },
      (error) => setDiagError(error),
      () => setIsDiagnosing(false),
    );
  };

  const handleCloseModal = () => {
    if (abortRef.current) {
      abortRef.current.abort();
    }
    setIsModalOpen(false);
    setIsDiagnosing(false);
  };

  if (!appsLoaded) {
    return (
      <Page>
        <PageSection>
          <EmptyState>
            <Spinner size="xl" />
            <EmptyStateBody>Loading ArgoCD Applications...</EmptyStateBody>
          </EmptyState>
        </PageSection>
      </Page>
    );
  }

  if (appsError) {
    return (
      <Page>
        <PageSection>
          <Alert variant="danger" title="Failed to load ArgoCD Applications">
            {String(appsError)}
          </Alert>
        </PageSection>
      </Page>
    );
  }

  return (
    <Page>
      <PageSection variant="default">
        <Split hasGutter>
          <SplitItem isFilled>
            <Title headingLevel="h1">ArgoAI</Title>
            <p style={{ marginTop: '0.5rem', opacity: 0.8 }}>
              AI-powered diagnostics for ArgoCD applications
            </p>
          </SplitItem>
        </Split>
      </PageSection>

      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem>
              <FormGroup label="LLM Provider" fieldId="provider">
                <FormSelect
                  id="provider"
                  value={provider}
                  onChange={(_e, val) => setProvider(val)}
                  style={{ minWidth: '200px' }}
                >
                  {PROVIDERS.map((p) => (
                    <FormSelectOption key={p.value} value={p.value} label={p.label} />
                  ))}
                </FormSelect>
              </FormGroup>
            </ToolbarItem>
            {provider !== 'ollama' && (
              <ToolbarItem>
                <FormGroup label="API Key" fieldId="apikey">
                  <TextInput
                    id="apikey"
                    type="password"
                    value={apiKey}
                    onChange={(_e, val) => setApiKey(val)}
                    placeholder="Enter API key"
                    style={{ minWidth: '300px' }}
                  />
                </FormGroup>
              </ToolbarItem>
            )}
          </ToolbarContent>
        </Toolbar>

        <Table aria-label="ArgoCD Applications" variant="compact">
          <Thead>
            <Tr>
              <Th>Name</Th>
              <Th>Namespace</Th>
              <Th>Health</Th>
              <Th>Sync</Th>
              <Th>Destination</Th>
              <Th>Action</Th>
            </Tr>
          </Thead>
          <Tbody>
            {(apps || []).map((app) => {
              const health = app.status?.health?.status;
              const sync = app.status?.sync?.status;
              const destNs = app.spec?.destination?.namespace || 'default';

              return (
                <Tr key={`${app.metadata.namespace}/${app.metadata.name}`}>
                  <Td dataLabel="Name">
                    <strong>{app.metadata.name}</strong>
                  </Td>
                  <Td dataLabel="Namespace">{app.metadata.namespace}</Td>
                  <Td dataLabel="Health">
                    <Label color={healthColor(health)} isCompact>
                      {health || 'Unknown'}
                    </Label>
                  </Td>
                  <Td dataLabel="Sync">
                    <Label color={syncColor(sync)} isCompact>
                      {sync || 'Unknown'}
                    </Label>
                  </Td>
                  <Td dataLabel="Destination">{destNs}</Td>
                  <Td dataLabel="Action">
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => handleDiagnose(app)}
                      isDisabled={!apiKey && provider !== 'ollama'}
                    >
                      Diagnose
                    </Button>
                  </Td>
                </Tr>
              );
            })}
          </Tbody>
        </Table>

        {(!apps || apps.length === 0) && (
          <EmptyState>
            <EmptyStateBody>
              No ArgoCD Applications found. Deploy an application to get started.
            </EmptyStateBody>
          </EmptyState>
        )}
      </PageSection>

      {isModalOpen && selectedApp && (
        <Modal
          variant={ModalVariant.large}
          isOpen={isModalOpen}
          onClose={handleCloseModal}
          aria-label="Diagnosis"
        >
          <ModalHeader
            title={`Diagnosing: ${selectedApp.metadata.name}`}
            description={`Using ${PROVIDERS.find((p) => p.value === provider)?.label || provider}`}
          />
          <ModalBody>
            <DiagnosisPanel
              events={diagEvents}
              isRunning={isDiagnosing}
              result={diagResult}
              agentName={agentName}
              error={diagError}
            />
          </ModalBody>
        </Modal>
      )}
    </Page>
  );
};

export default ArgoAgentPage;
