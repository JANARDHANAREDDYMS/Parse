import type { MockDocument } from './types';

export interface ApiDocument { document_id:string; original_filename:string; status:string; created_at:string; updated_at:string; business_status:string|null; current_stage:string; item_count:number|null; review_issue_count:number|null }
export interface Pipeline { document_id:string; preprocessing:any; document_analysis:any; sku_mapping:{total:number;completed:number;failed:number;review:number;candidates:any[]}; term_applicability:any; normalization:any; document_status:string }
export class MaximorApi {
  constructor(private readonly baseUrl:string){ }
  private async request<T>(path:string, init?:RequestInit):Promise<T>{const response=await fetch(`${this.baseUrl}${path}`,init);if(!response.ok) throw new Error(`API request failed (${response.status})`);return response.json() as Promise<T>}
  listDocuments(org:string){return this.request<{documents:ApiDocument[]}>(`/v1/organizations/${org}/documents`)}
  pipeline(org:string,id:string){return this.request<Pipeline>(`/v1/organizations/${org}/documents/${id}/pipeline`)}
  upload(org:string,file:File){const body=new FormData();body.append('file',file);return this.request<{document_id:string;job_id:string;document_status:string;job_status:string}>(`/v1/organizations/${org}/documents`,{method:'POST',body})}
}
export const apiBaseUrl=import.meta.env.VITE_MAXIMOR_API_BASE_URL||'http://127.0.0.1:8000';
