/** Pure projection from the persisted normalization shape to the assignment JSON shape. */
export interface AssignmentExtraction {
  contract_items_count: number;
  contract_items: Array<Record<string, unknown>>;
}

const numberOrNull = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value.replace(/[$,]/g, '')))) return Number(value.replace(/[$,]/g, ''));
  return null;
};

/** Maps a normalization result; unresolved or unsupported fields remain null. */
export function toAssignmentExtraction(result: any | null | undefined): AssignmentExtraction {
  const source = Array.isArray(result?.line_items) ? result.line_items : [];
  const contract_items = source.map((item: any) => {
    const money = item.unit_price && typeof item.unit_price === 'object' ? item.unit_price : {};
    const total = item.total_listed_value && typeof item.total_listed_value === 'object' ? item.total_listed_value : {};
    return {
      name: item.sku_name ?? item.name ?? null,
      sku_reference: item.sku_code ?? null,
      currency: money.currency ?? total.currency ?? item.currency ?? null,
      total_listed_value: numberOrNull(total.amount ?? item.total_listed_value),
      quantity: numberOrNull(item.quantity?.amount ?? item.quantity),
      unit_price: numberOrNull(money.amount ?? item.unit_price),
      unit_price_period: item.unit_price_period ?? null,
      service_start_date: item.service_start_date ?? null,
      service_end_date: item.service_end_date ?? null,
      invoicing_schedule_type: item.invoicing_schedule_type ?? null,
      invoicing_frequency: item.invoicing_frequency ?? null,
      payment_terms: item.payment_terms ?? null,
      special_notes: typeof item.special_notes === 'string' ? item.special_notes : null,
    };
  });
  return { contract_items_count: contract_items.length, contract_items };
}
