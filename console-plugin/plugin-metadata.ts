import type { ConsolePluginBuildMetadata } from '@openshift-console/dynamic-plugin-sdk-webpack';

const metadata: ConsolePluginBuildMetadata = {
  name: 'argocd-agent-plugin',
  version: '0.1.0',
  displayName: 'ArgoAI',
  description: 'AI-powered diagnostic agent for ArgoCD applications',
  dependencies: {
    '@console/pluginAPI': '*',
  },
  exposedModules: {
    ArgoAgentPage: './src/components/ArgoAgentPage',
  },
};

export default metadata;
