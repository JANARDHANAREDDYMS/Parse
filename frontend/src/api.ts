import type { MockDocument } from './types';

export interface ApiDocument { document_id:string; original_filename:string; status:string; created_at:string; updated_at:string; business_status:string|null; current_stage:string; item_count:number|null; review_issue_count:number|null }
export interface Pipeline { document_id:string; preprocessing:any; document_analysis:any; sku_mapping:{total:number;completed:number;failed:number;review:number;candidates:any[]}; term_applicability:any; normalization:any; document_status:string }
export interface EvidencePresentation { field:string; page_number:number; evidence_id:string; evidence_type:'text'|'table'; source_description:string; excerpt:string|null; render_url:string }
export class MaximorApi {
  constructor(private readonly baseUrl:string){ }
  private async request<T>(path:string, init?:RequestInit):Promise<T>{const response=await fetch(`${this.baseUrl}${path}`,init);if(!response.ok) throw new Error(`API request failed (${response.status})`);return response.json() as Promise<T>}
  listDocuments(org:string){return this.request<{documents:ApiDocument[]}>(`/v1/organizations/${org}/documents`)}
  pipeline(org:string,id:string){return this.request<Pipeline>(`/v1/organizations/${org}/documents/${id}/pipeline`)}
  evidence(org:string,id:string){return this.request<{document_id:string;preprocessing_run_id:string;evidence:EvidencePresentation[]}>(`/v1/organizations/${org}/documents/${id}/evidence`)}
  upload(org:string,file:File){const body=new FormData();body.append('file',file);return this.request<{document_id:string;job_id:string;document_status:string;job_status:string}>(`/v1/organizations/${org}/documents`,{method:'POST',body})}
}
// Port 8002 is the local dashboard default; VITE_MAXIMOR_API_BASE_URL still overrides it.
export const apiBaseUrl=import.meta.env.VITE_MAXIMOR_API_BASE_URL||'http://127.0.0.1:8002';
