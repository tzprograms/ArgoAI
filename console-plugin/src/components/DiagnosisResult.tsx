import * as React from 'react';
import {
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Label,
  Title,
  Divider,
} from '@patternfly/react-core';
import {
  ExclamationCircleIcon,
  WrenchIcon,
  SearchIcon,
} from '@patternfly/react-icons';
import type { DiagnosisResult as DiagnosisResultType } from '../utils/api';

interface DiagnosisResultProps {
  result: DiagnosisResultType;
  agentName?: string;
}

const DiagnosisResultView: React.FC<DiagnosisResultProps> = ({
  result,
  agentName,
}) => {
  return (
    <div className="argoai-diagnosis-result">
      <Card isCompact>
        <CardTitle>
          <Title headingLevel="h3">Diagnosis Complete</Title>
        </CardTitle>
        <CardBody>
          <DescriptionList isHorizontal>
            <DescriptionListGroup>
              <DescriptionListTerm>
                <ExclamationCircleIcon /> Error
              </DescriptionListTerm>
              <DescriptionListDescription>
                <strong>{result.error}</strong>
              </DescriptionListDescription>
            </DescriptionListGroup>

            <DescriptionListGroup>
              <DescriptionListTerm>
                <SearchIcon /> Root Cause
              </DescriptionListTerm>
              <DescriptionListDescription>
                {result.cause}
              </DescriptionListDescription>
            </DescriptionListGroup>

            {result.confidence && (
              <DescriptionListGroup>
                <DescriptionListTerm>Confidence</DescriptionListTerm>
                <DescriptionListDescription>
                  <Label
                    color={
                      result.confidence === 'high'
                        ? 'green'
                        : result.confidence === 'medium'
                        ? 'orange'
                        : 'red'
                    }
                  >
                    {result.confidence}
                  </Label>
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
          </DescriptionList>

          <Divider className="pf-v6-u-my-md" />

          <Card isPlain isCompact>
            <CardTitle>
              <WrenchIcon /> Recommended Fix
            </CardTitle>
            <CardBody>
              <p style={{ whiteSpace: 'pre-wrap' }}>{result.fix}</p>
            </CardBody>
          </Card>

          {result.evidence && (
            <>
              <Divider className="pf-v6-u-my-md" />
              <Card isPlain isCompact>
                <CardTitle>Evidence</CardTitle>
                <CardBody>
                  <pre
                    style={{
                      whiteSpace: 'pre-wrap',
                      fontSize: '0.85em',
                      background: 'var(--pf-t--global--background--color--secondary--default)',
                      padding: '1rem',
                      borderRadius: '4px',
                      maxHeight: '200px',
                      overflow: 'auto',
                    }}
                  >
                    {result.evidence}
                  </pre>
                </CardBody>
              </Card>
            </>
          )}

          {agentName && (
            <div style={{ marginTop: '1rem', fontSize: '0.85em', opacity: 0.7 }}>
              Diagnosed by: {agentName}
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
};

export default DiagnosisResultView;
