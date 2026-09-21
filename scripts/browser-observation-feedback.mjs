/** Transient observation reuse and value-free, bounded host diagnostics. */
export const ACTION_READBACK_MAX_AGE_MS = 250;

const structure = form => JSON.stringify([
  form.coverage, form.protected_count,
  form.fields.map(field => [field.field_key, field.selector, field.label, field.group_key,
    field.group, field.control, field.required, field.disabled, field.readonly,
    field.options, field.files]),
]);

/** Reuse only one successful same-page readback, never a TTL cache across turns. */
export function canReuseActionReadback(pending, actionResult, pageUrl, now) {
  if (!pending || pending !== actionResult) return false;
  const age = now - pending.observedAt;
  const { before, form, report } = pending;
  return Number.isFinite(age) && age >= 0 && age <= ACTION_READBACK_MAX_AGE_MS &&
    before?.page_url === pageUrl && form?.page_url === pageUrl &&
    Array.isArray(before.fields) && Array.isArray(form.fields) &&
    report?.persisted === true && report.invalid === false &&
    !form.fields.some(field => field.value_source === 'unavailable' ||
      (field.invalid === true && before.fields.find(old => old.field_key === field.field_key)?.invalid !== true)) &&
    structure(before) === structure(form);
}

/** Coverage is descriptive. Neither zero iframes nor zero empty fields proves completeness. */
export function observationCoverage(form) {
  const count = value => Number.isInteger(value) && value >= 0 ? value : null;
  return {
    scope: form?.coverage?.scope || 'unknown',
    field_count: Array.isArray(form?.fields) ? form.fields.length : null,
    unknown_value_count: Array.isArray(form?.fields)
      ? form.fields.filter(field => field.value_source === 'unavailable').length : null,
    protected_count: count(form?.protected_count),
    iframe_count: count(form?.coverage?.iframe_count),
    open_shadow_count: count(form?.coverage?.open_shadow_count),
    frame_contents: 'not_inspected',
    all_steps: 'unverified',
  };
}

const numeric = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const measures = ['queue_wait_ms', 'action_ms', 'observation_ms', 'host_service_ms', 'text_bytes', 'image_bytes'];
const outcomes = new Set(['completed', 'failed', 'outcome_unknown', 'rejected']);

/** Queue time excludes model thinking. Missing or future wall timestamps stay unknown. */
export function queueWaitMs(createdAt, nowSeconds) {
  return numeric(createdAt) !== null && numeric(nowSeconds) !== null && createdAt <= nowSeconds
    ? (nowSeconds - createdAt) * 1000 : null;
}

/** Count payload bytes without retaining any content, values, URLs, paths or image data. */
export function observationSizes(content) {
  let textBytes = 0;
  let imageBytes = 0;
  let reused = false;
  for (const block of content || []) {
    if (block.type === 'text' && typeof block.text === 'string') {
      textBytes += Buffer.byteLength(block.text, 'utf8');
      try {
        reused ||= JSON.parse(block.text)?.observation_feedback?.form_readback_reused === true;
      } catch { /* Ordinary page text need not be JSON. */ }
    } else if (block.type === 'image' && typeof block.data === 'string') {
      imageBytes += Buffer.byteLength(block.data, 'base64');
    }
  }
  return { text_bytes: textBytes, image_bytes: imageBytes, form_readback_reused: reused };
}

/** Lifetime counters plus nearest-rank percentiles over the last bounded window only. */
export class BridgeMetrics {
  #samples = [];
  #limit;
  #totals = { operations: 0, completed: 0, failed: 0, outcome_unknown: 0, rejected: 0, form_readbacks_reused: 0 };

  constructor(limit = 128) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1024) throw new TypeError('Invalid metrics window');
    this.#limit = limit;
  }

  record(sample) {
    // An allowlist prevents accidental collection of request arguments or page content.
    const outcome = outcomes.has(sample.outcome) ? sample.outcome : 'outcome_unknown';
    const item = Object.fromEntries(measures.map(key => [key, numeric(sample[key])]));
    item.outcome = outcome;
    item.form_readback_reused = sample.form_readback_reused === true;
    this.#samples.push(item);
    if (this.#samples.length > this.#limit) this.#samples.shift();
    this.#totals.operations++;
    this.#totals[outcome]++;
    if (item.form_readback_reused) this.#totals.form_readbacks_reused++;
  }

  snapshot() {
    const recent = {};
    for (const name of measures) {
      const values = this.#samples.map(item => item[name]).filter(value => value !== null).sort((a, b) => a - b);
      const percentile = fraction => values.length ? values[Math.ceil(values.length * fraction) - 1] : null;
      recent[name] = { measured: values.length, unavailable: this.#samples.length - values.length,
        p50: percentile(0.50), p95: percentile(0.95), max: values.length ? values.at(-1) : null };
    }
    return { schema_version: 'applypilot-attended-host-metrics/v1',
      lifetime: { ...this.#totals }, window_limit: this.#limit, window_size: this.#samples.length,
      recent, scope: 'host_service_only', model_time: 'unavailable', submission_success: 'not_measured' };
  }
}
