/** Bounded preparation on one observed page. No clicks, uploads or submission. */
import { operateObservedControl, ControlNotReady } from './browser-form-state.mjs';

const textControls = new Set(['text', 'textarea', 'email', 'tel', 'url', 'search', 'number']);
const shape = form => JSON.stringify([form.page_url, form.protected_count, form.fields.map(f =>
  [f.field_key, f.selector, f.label, f.group_key, f.group, f.control, f.disabled, f.readonly, f.options])]);
const value = f => JSON.stringify([f.value, f.checked, f.files, f.selected_display]);

export function validateFieldBatch(snapshot, steps) {
  if (!snapshot || !Array.isArray(steps) || steps.length < 1 || steps.length > 4) {
    throw new ControlNotReady('Field batch requires an observation and one to four routine fields');
  }
  const seen = new Set();
  for (const step of steps) {
    if (!step || !['fill_control', 'select_control'].includes(step.operation) ||
        Object.keys(step).sort().join(',') !== 'field_key,operation,value' ||
        typeof step.value !== 'string' || step.value.length > 12000 || seen.has(step.field_key)) {
      throw new ControlNotReady('Batch requires unique observed text/native-select fields');
    }
    seen.add(step.field_key);
    const field = snapshot.fields.find(f => f.field_key === step.field_key);
    if (!field || field.disabled || field.readonly || field.value_source === 'unavailable' ||
        (step.operation === 'fill_control' ? !textControls.has(field.control) : field.control !== 'select')) {
      throw new ControlNotReady('Batch contains an unsupported or unavailable control');
    }
    if (step.operation === 'select_control' && field.options.filter(o => !o.disabled &&
        (o.value === step.value || o.label === step.value)).length !== 1) {
      throw new ControlNotReady('Batch option is not uniquely observed');
    }
  }
}

export async function operateFieldBatch(tab, snapshot, steps, operate = operateObservedControl) {
  validateFieldBatch(snapshot, steps); // Validate the whole plan before any write.
  let current = snapshot;
  const results = [];
  for (const step of steps) {
    let result;
    try {
      result = await operate(tab, current, step.operation, { field_key: step.field_key, value: step.value });
    } catch (error) {
      if (!(error instanceof ControlNotReady)) throw error; // Unknown write outcome stops the host.
      if (!results.length) throw error;
      return { observation: current, batch_result: { status: 'parked', completed: results.length,
        requested: steps.length, results, reason: 'control_changed', reobserve_required: true } };
    }
    const { observation: after, ...report } = result;
    results.push(report);
    const unexpectedChange = shape(current) !== shape(after) || current.fields.some(f =>
      f.field_key !== step.field_key && value(f) !== value(after.fields.find(x => x.field_key === f.field_key) || {}));
    current = after;
    if (report.persisted !== true || report.invalid === true || unexpectedChange) {
      return { observation: current, batch_result: { status: 'parked', completed: results.length,
        requested: steps.length, results, reason: unexpectedChange ? 'form_changed' : 'readback_not_verified',
        reobserve_required: true } };
    }
  }
  return { observation: current, batch_result: { status: 'verified', completed: results.length,
    requested: steps.length, results, immediate_readback_only: true } };
}
