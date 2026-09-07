export type Status = 'processing' | 'completed' | 'review_required' | 'failed_validation' | 'queued';
export type StageState = 'completed' | 'running' | 'queued' | 'failed' | 'review';
export interface Evidence { page: number; id: string; kind: 'block' | 'table'; excerpt: string; supports: string; }
export interface LineItem { sku: string; name: string; quantity?: string; currency?: string; unitPrice?: string; total?: string; period?: string; billing?: string; payment?: string; review?: boolean; evidence: Evidence[]; }
export interface PipelineStage { name: string; state: StageState; duration: string; detail?: string; }
export interface MockDocument { id: string; filename: string; customer: string; uploaded: string; status: Status; stage: string; itemCount: number; pages: number; stages: PipelineStage[]; lineItems: LineItem[]; evidence: Evidence[]; note?: string; normalizationResult?: unknown; reviewIssues?: string[]; }
export interface Batch { name: string; documents: MockDocument[]; }
