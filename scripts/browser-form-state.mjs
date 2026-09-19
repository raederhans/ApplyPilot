/** DOM-backed form observation and bounded, observed-control operations for IAB.
 * Values are transient host observations, not applicant facts or submit authority.
 */
export async function observeForm(tab) {
  const form = await tab.playwright.evaluate(() => {
    const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
    const labelledText = element => clean([element.textContent,
      ...[...element.querySelectorAll('slot')].flatMap(slot => slot.assignedNodes({ flatten: true }).map(node => node.textContent)),
    ].join(' '));
    const quote = value => String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    const roots = [document];
    const rootFor = new Map();
    const hostFor = new Map();
    const elements = [];
    const idCounts = new Map();
    const tagIdCounts = new Map();
    for (let i = 0; i < roots.length; i++) {
      for (const el of roots[i].querySelectorAll('*')) {
        rootFor.set(el, roots[i]);
        elements.push(el);
        if (el.id) {
          idCounts.set(el.id, (idCounts.get(el.id) || 0) + 1);
          const key = `${el.tagName}/${el.id}`;
          tagIdCounts.set(key, (tagIdCounts.get(key) || 0) + 1);
        }
        if (el.shadowRoot) { roots.push(el.shadowRoot); hostFor.set(el.shadowRoot, el); }
      }
    }
    const selectorFor = element => {
      if (element.id) {
        const selector = `[id="${quote(element.id)}"]`;
        if (idCounts.get(element.id) === 1) return selector;
        if (tagIdCounts.get(`${element.tagName}/${element.id}`) === 1) return element.tagName.toLowerCase() + selector;
      }
      const parts = [];
      for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
        const root = rootFor.get(node);
        const siblings = [...(node.parentElement?.children || root?.children || [node])].filter(x => x.tagName === node.tagName);
        parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${siblings.indexOf(node) + 1})`);
        if (!node.parentElement && hostFor.has(root)) return selectorFor(hostFor.get(root)) + ' ' + parts.join(' > ');
      }
      return parts.join(' > ');
    };
    const nodes = elements.filter(el => el.matches('input, textarea, select, [role="combobox"]'));
    const fields = [];
    let protectedCount = 0;
    for (const el of nodes) {
      if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
      const root = rootFor.get(el);
      const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => root.getElementById(id)?.textContent || '').join(' ');
      const labels = [...(el.labels || [])].map(labelledText).join(' ');
      const label = clean(el.getAttribute('aria-label') || labelled || labels || el.getAttribute('placeholder') || el.name || el.id);
      const identity = `${type} ${label} ${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`;
      if (/password|passcode|one.time|\botp\b|verification.code|security.code|passport|\bnric\b|\bssn\b|\bfin\b|national.id|credit.card|bank.account|consent|declaration|terms|privacy|agree|accept/i.test(identity)) {
        protectedCount++;
        continue;
      }
      const group = el.closest('fieldset, [role="group"]');
      const groupLabel = group ? clean(group.getAttribute('aria-label') || group.querySelector('legend')?.textContent) : '';
      const control = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : el.getAttribute('role') === 'combobox' ? 'combobox' : type || 'text';
      let options = control === 'select' ? [...el.options].map(o => ({ value: o.value, label: clean(o.label), disabled: o.disabled || o.parentElement?.disabled === true })) : [];
      if (control === 'combobox') {
        const lists = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean).map(id => {
          for (let scope = root; scope; scope = rootFor.get(hostFor.get(scope))) {
            const matches = [...scope.querySelectorAll(`[id="${quote(id)}"]`)];
            if (matches.length) return matches.length === 1 ? matches[0] : null;
          }
          return null;
        }).filter(Boolean);
        const inList = option => {
          for (let node = option; node; node = node.parentElement || hostFor.get(rootFor.get(node))) {
            if (lists.includes(node)) return true;
          }
          return false;
        };
        const optionLabel = option => {
          const own = clean(option.getAttribute('aria-label') || labelledText(option));
          if (own) return own;
          const scope = rootFor.get(option);
          const host = hostFor.get(scope);
          return host && scope.querySelectorAll('[role="option"]').length === 1 ? clean(host.textContent) : '';
        };
        options = elements.filter(o => o.getAttribute('role') === 'option' && inList(o))
          .filter(o => o.getClientRects().length && getComputedStyle(o).visibility !== 'hidden')
          .map(o => ({ value: optionLabel(o), label: optionLabel(o), selector: selectorFor(o) + '[role="option"]',
            selected: o.getAttribute('aria-selected') === 'true', disabled: o.getAttribute('aria-disabled') === 'true' }))
          .filter(o => o.label);
      }
      const key = selectorFor(el);
      // React Select keeps its search input empty after committing an option.
      // Preserve the visible selection separately, rather than calling it blank.
      const selectedDisplay = control === 'combobox'
        ? [...(el.closest('.select__value-container')?.querySelectorAll('.select__single-value, .select__multi-value') || [])].map(o => clean(o.textContent))
        : [];
      fields.push({ field_key: key, selector: key, label, group: groupLabel,
        group_key: group ? selectorFor(group) : '', control,
        selected_display: selectedDisplay,
        value: control === 'file' ? '' : String(el.getAttribute('aria-valuetext') ?? el.value ?? ''),
        checked: ['checkbox', 'radio'].includes(control) ? el.checked : null,
        focused: el === root.activeElement,
        required: el.required === true || el.getAttribute('aria-required') === 'true',
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true', readonly: el.readOnly === true,
        invalid: el.validity?.valid === false || el.getAttribute('aria-invalid') === 'true',
        validation_message: el.validationMessage || '', options,
        files: control === 'file' ? [...(el.files || [])].map(f => ({ name: f.name, size: f.size })) : [],
      });
    }
    return { page_url: location.href, fields, protected_count: protectedCount,
      coverage: { scope: 'visible_top_document_open_shadow', open_shadow_count: roots.length - 1,
        iframe_count: elements.filter(el => el.tagName === 'IFRAME').length } };
  });
  // The IAB read-only DOM scope can omit live input properties. Use its supported
  // DOM snapshot capability, never a page script mutation or a browser side channel.
  if (typeof tab.capabilities?.get === 'function') {
    const cdp = await tab.capabilities.get('cdp');
    const snapshot = await cdp.send('DOMSnapshot.captureSnapshot', { computedStyles: [] });
    enrichLiveValues(form, snapshot);
  }
  return form;
}

export function enrichLiveValues(form, snapshot) {
  const strings = snapshot.strings || [];
  const doc = snapshot.documents?.find(item => strings[item.documentURL] === form.page_url);
  // This read may run after an input; never classify its failure as proven no-op.
  if (!doc) throw new Error('Live form document changed; observe again');
  const nodes = doc.nodes;
  const attrs = (nodes.attributes || []).map(row => Object.fromEntries(Array.from({ length: row.length / 2 }, (_, i) => [strings[row[i * 2]], strings[row[i * 2 + 1]]])));
  const quote = value => String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const selectors = new Map();
  const paths = [];
  const canonical = [];
  const ids = new Map();
  const tagIds = new Map();
  const siblings = new Map();
  for (let i = 0; i < attrs.length; i++) if (attrs[i].id) {
    ids.set(attrs[i].id, (ids.get(attrs[i].id) || 0) + 1);
    const key = `${nodes.nodeName[i]}/${attrs[i].id}`;
    tagIds.set(key, (tagIds.get(key) || 0) + 1);
  }
  for (let index = 0; index < nodes.nodeType.length; index++) {
    if (nodes.nodeType[index] !== 1) continue;
    const name = strings[nodes.nodeName[index]].toLowerCase();
    const parent = nodes.parentIndex[index];
    const siblingKey = `${parent}/${nodes.nodeName[index]}`;
    const ordinal = (siblings.get(siblingKey) || 0) + 1;
    siblings.set(siblingKey, ordinal);
    const shadow = nodes.nodeType[parent] === 11;
    paths[index] = [shadow ? canonical[nodes.parentIndex[parent]] : paths[parent], `${name}:nth-of-type(${ordinal})`].filter(Boolean).join(shadow ? ' ' : ' > ');
    canonical[index] = paths[index];
    selectors.set(paths[index], index);
    if (attrs[index]?.id && tagIds.get(`${nodes.nodeName[index]}/${attrs[index].id}`) === 1) {
      canonical[index] = `${name}[id="${quote(attrs[index].id)}"]`;
      selectors.set(canonical[index], index);
    }
    if (attrs[index]?.id && ids.get(attrs[index].id) === 1) {
      canonical[index] = `[id="${quote(attrs[index].id)}"]`;
      selectors.set(canonical[index], index);
    }
  }
  const rare = key => new Map((nodes[key]?.index || []).map((index, i) => [index, strings[nodes[key].value[i]] ?? '']));
  const values = rare('inputValue');
  const textarea = rare('textValue');
  for (const field of form.fields) {
    if (field.control === 'file') continue;
    const index = selectors.get(field.selector);
    const source = field.control === 'textarea' ? textarea : values;
    if (index !== undefined && source.has(index)) {
      field.value = source.get(index);
      field.value_source = 'live_dom_snapshot';
    } else if (['text', 'textarea', 'email', 'tel', 'url', 'search', 'number', 'date', 'month'].includes(field.control)) {
      field.value = null;
      field.value_source = 'unavailable';
    } else field.value_source = 'dom_read';
    if (['checkbox', 'radio'].includes(field.control) && index !== undefined) {
      field.checked = (nodes.inputChecked?.index || []).includes(index);
    }
  }
}

const identity = field => JSON.stringify([field.selector, field.label, field.group_key, field.group, field.control]);
const selectedDisplay = field => Array.isArray(field.selected_display) ? field.selected_display.filter(value => typeof value === 'string') : [];
const stateValue = field => {
  if (field.control === 'combobox') {
    const display = selectedDisplay(field);
    if (display.length === 1) return display[0];
    if (display.length > 1) return display;
  }
  return ['checkbox', 'radio'].includes(field.control) ? field.checked : field.value;
};
const sameStateValue = (left, right) => JSON.stringify(left) === JSON.stringify(right);

export function changedFields(before, after) {
  if (!before || before.page_url !== after.page_url) return [];
  const previous = new Map(before.fields.map(field => [field.field_key, field]));
  return after.fields.flatMap(field => {
    const old = previous.get(field.field_key);
    if (!old || identity(old) !== identity(field) || field.control === 'file') return [];
    const displayChanged = JSON.stringify(selectedDisplay(old)) !== JSON.stringify(selectedDisplay(field));
    if (((!displayChanged && old.value_source === 'unavailable') || (!displayChanged && field.value_source === 'unavailable')) ||
      (!displayChanged && sameStateValue(stateValue(old), stateValue(field)))) return [];
    return [{ field_key: field.field_key, label: field.label, group: field.group,
      previous_value: stateValue(old), observed_value: stateValue(field),
      requires_fact_check: true }];
  });
}

export async function operateObservedControl(tab, snapshot, operation, args) {
  if (!snapshot) throw new ControlNotReady('Observe form controls before input');
  const old = snapshot.fields.find(field => field.field_key === args.field_key);
  if (!old) throw new ControlNotReady('Control is not in the current form observation');
  const fresh = await observeForm(tab);
  const field = fresh.fields.find(item => item.field_key === args.field_key);
  if (fresh.page_url !== snapshot.page_url || !field || identity(field) !== identity(old)) throw new ControlNotReady('Form control changed; observe again before input');
  if (field.disabled || field.readonly) throw new ControlNotReady('Control is not writable');
  const locator = tab.playwright.locator(field.selector);
  if (await locator.count() !== 1) throw new ControlNotReady('Form control is ambiguous');
  let expected;
  if (operation === 'fill_control') {
    if (!['text', 'textarea', 'email', 'tel', 'url', 'search', 'number', 'date', 'month'].includes(field.control)) throw new ControlNotReady('Use an appropriate control operation');
    if (typeof args.value !== 'string' || args.value.length > 12000) throw new ControlNotReady('Invalid control value');
    expected = args.value;
    await locator.fill(expected, { timeoutMs: 10000 });
    await locator.press('Tab', { timeoutMs: 10000 });
    // Native date inputs can have several keyboard segments. One Tab may move
    // within the input without firing blur; verify focus before advancing again.
    if (['date', 'month'].includes(field.control)) {
      for (let remaining = 3; remaining > 0; remaining--) {
        const focus = await observeForm(tab);
        if (!focus.fields.find(item => item.field_key === field.field_key)?.focused) break;
        await locator.press('Tab', { timeoutMs: 10000 });
      }
    }
  } else if (operation === 'select_control') {
    if (!['select', 'combobox'].includes(field.control) || typeof args.value !== 'string') throw new ControlNotReady('Select requires an observed select or combobox option');
    if (JSON.stringify(field.options) !== JSON.stringify(old.options)) throw new ControlNotReady('Options changed; observe options again');
    const options = field.options.filter(o => !o.disabled && (o.value === args.value || o.label === args.value));
    if (options.length !== 1) throw new ControlNotReady('Option is missing or ambiguous; observe options again');
    expected = options[0].value;
    if (field.control === 'select') {
      await locator.selectOption({ value: expected }, { timeoutMs: 10000 });
      await locator.press('Tab', { timeoutMs: 10000 });
    } else {
      const optionLocator = tab.playwright.locator(options[0].selector);
      if (await optionLocator.count() !== 1) throw new ControlNotReady('Combobox option is ambiguous');
      await optionLocator.click({ timeoutMs: 10000 });
    }
  } else if (operation === 'set_checked') {
    if (field.control !== 'checkbox' || typeof args.checked !== 'boolean') throw new ControlNotReady('Checked state requires an ordinary observed checkbox');
    expected = args.checked;
    await locator.setChecked(expected, { timeoutMs: 10000 });
  } else throw new ControlNotReady('Unsupported form operation');
  const after = await observeForm(tab);
  const actual = after.fields.find(item => item.field_key === field.field_key);
  let persisted = after.page_url === fresh.page_url && !!actual && identity(actual) === identity(field);
  if (persisted && field.control === 'combobox') {
    const display = selectedDisplay(actual);
    // A combobox with several visible selections has ambiguous replace/add
    // semantics. Keep the result unknown instead of claiming a replacement.
    if (display.length > 1) persisted = null;
    else if (display.length === 1) {
      // Menus commonly unmount after selection; verify against the exact option
      // observed before the click, not the now-closed popup's option list.
      const selected = field.options.filter(option => !option.disabled &&
        (option.value === expected || option.label === expected));
      persisted = selected.length === 1 &&
        (display[0] === selected[0].label || display[0] === selected[0].value || display[0] === expected);
    } else persisted = actual.value === expected;
  } else if (persisted) persisted = stateValue(actual) === expected;
  const displayProvesSingleComboboxSelection = actual?.control === 'combobox' && selectedDisplay(actual).length === 1 && persisted === true;
  return { field_key: field.field_key, operation,
    persisted: actual?.value_source === 'unavailable' && !displayProvesSingleComboboxSelection ? null : persisted,
    invalid: actual?.invalid ?? null,
    validation_message: actual?.validation_message || '',
    observation: after };
}

export class ControlNotReady extends Error {}
