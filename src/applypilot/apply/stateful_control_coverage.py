"""Fail-closed coverage proof for checkbox, radio, and toggle controls."""

from __future__ import annotations

from collections.abc import Mapping

STATEFUL_CONTROL_COVERAGE_SCHEMA_VERSION = 1
STATEFUL_CONTROL_COVERAGE_LIMIT = 200
_STATEFUL_CONTROL_COVERAGE_KEYS = frozenset(
    {
        "schema_version",
        "discovered_count",
        "classified_visible_native_count",
        "unclassified_count",
        "selected_or_filled_count",
        "overflow",
        "proof_complete",
    }
)

STATEFUL_CONTROL_COVERAGE_SCRIPT = r"""() => {
  const limit = 200;
  const roots = [document];
  const elements = [];
  for (let index = 0; index < roots.length; index += 1) {
    const descendants = [...roots[index].querySelectorAll('*')];
    elements.push(...descendants);
    for (const element of descendants) {
      if (element.shadowRoot) roots.push(element.shadowRoot);
    }
  }
  const rendered = (element) => {
    const view = element.ownerDocument && element.ownerDocument.defaultView;
    if (!view) return false;
    const style = view.getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      element.getClientRects().length > 0;
  };
  const nativeActive = (element) => !element.matches(':disabled');
  const selected = (element) => element.checked === true ||
    ['true', 'mixed'].includes(
      String(element.getAttribute('aria-checked') || '').toLowerCase()
    ) || ['true', 'mixed'].includes(
      String(element.getAttribute('aria-pressed') || '').toLowerCase()
    );
  const required = (element) => element.required === true ||
    String(element.getAttribute('aria-required') || '').toLowerCase() === 'true';
  const proxySelected = (element) => [...(element.labels || [])]
    .filter(rendered)
    .some(selected);
  const smartRecruitersLabelProxy = (element) => {
    if (location.protocol !== 'https:' || location.hostname.toLowerCase() !== 'jobs.smartrecruiters.com') {
      return false;
    }
    if (String(element.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
    const labels = [...(element.labels || [])].filter(rendered);
    return Boolean(element.id) && labels.length === 1 &&
      String(labels[0].innerText || labels[0].textContent || '').replace(/\s+/g, ' ').trim().length > 0;
  };
  const smartRecruitersCustomProxy = (element) => {
    if (location.protocol !== 'https:' || location.hostname.toLowerCase() !== 'jobs.smartrecruiters.com') {
      return false;
    }
    if (!rendered(element) || String(element.getAttribute('aria-disabled') || '').toLowerCase() === 'true') {
      return false;
    }
    const roles = String(element.getAttribute('role') || '').toLowerCase().split(/\s+/);
    const checkedRole = roles.some((role) => ['checkbox', 'radio', 'switch'].includes(role));
    const stateAttribute = checkedRole ? 'aria-checked' : 'aria-pressed';
    const state = String(element.getAttribute(stateAttribute) || '').toLowerCase();
    let label = String(
      element.getAttribute('aria-label') || element.innerText || element.textContent || ''
    ).replace(/\s+/g, ' ').trim();
    for (let node = element.parentElement, depth = 0;
      !label && node && node !== document.body && depth < 6;
      node = node.parentElement, depth += 1) {
      const candidate = String(node.innerText || node.textContent || '')
        .replace(/\s+/g, ' ').trim();
      if (candidate && candidate.length <= 500) label = candidate;
    }
    return ['true', 'false', 'mixed'].includes(state) && label.length > 0;
  };
  const natives = elements.filter((element) =>
    element.matches('input[type="checkbox"],input[type="radio"]') &&
    nativeActive(element)
  );
  const visibleNatives = natives.filter(rendered);
  const hiddenNatives = natives.filter((element) => !rendered(element));
  const classifiedHiddenNatives = hiddenNatives.filter(smartRecruitersLabelProxy);
  const unclassifiedHiddenNatives = hiddenNatives.filter(
    (element) => !smartRecruitersLabelProxy(element)
  );
  const customCandidates = [...new Set(elements.filter((element) =>
    !element.matches('input[type="checkbox"],input[type="radio"]') &&
    element.matches(
      '[role~="checkbox" i],[role~="radio" i],[role~="switch" i],'
      + '[aria-checked],[aria-pressed]'
    )
  ))];
  const activeCustom = customCandidates.filter((element) =>
    rendered(element) || selected(element) || required(element)
  );
  const classifiedCustom = activeCustom.filter(smartRecruitersCustomProxy);
  const unclassifiedCustom = activeCustom.filter(
    (element) => !smartRecruitersCustomProxy(element)
  );
  const unclassified = [...unclassifiedHiddenNatives, ...unclassifiedCustom];
  const classifiedCount = visibleNatives.length + classifiedHiddenNatives.length + classifiedCustom.length;
  const discoveredCount = classifiedCount + unclassified.length;
  const selectedOrFilledCount = visibleNatives.filter(selected).length +
    hiddenNatives.filter((element) => selected(element) || proxySelected(element)).length +
    activeCustom.filter(selected).length;
  const overflow = discoveredCount > limit;
  return {
    schema_version: 1,
    discovered_count: discoveredCount,
    classified_visible_native_count: classifiedCount,
    unclassified_count: unclassified.length,
    selected_or_filled_count: selectedOrFilledCount,
    overflow,
    proof_complete: !overflow && unclassified.length === 0
  };
}"""


def stateful_control_coverage_error(report: Mapping[str, object]) -> str | None:
    """Validate a bounded host proof and reject every unclassified control."""

    coverage = report.get("stateful_control_coverage")
    if not isinstance(coverage, Mapping):
        return "stateful_control_coverage_unproven"
    if set(coverage) != _STATEFUL_CONTROL_COVERAGE_KEYS:
        return "stateful_control_coverage_unproven"
    schema_version = coverage.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version != STATEFUL_CONTROL_COVERAGE_SCHEMA_VERSION
    ):
        return "stateful_control_coverage_unproven"

    count_names = (
        "discovered_count",
        "classified_visible_native_count",
        "unclassified_count",
        "selected_or_filled_count",
    )
    counts: dict[str, int] = {}
    for name in count_names:
        value = coverage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return "stateful_control_coverage_unproven"
        counts[name] = value

    overflow = coverage.get("overflow")
    proof_complete = coverage.get("proof_complete")
    if not isinstance(overflow, bool) or not isinstance(proof_complete, bool):
        return "stateful_control_coverage_unproven"
    if counts["discovered_count"] != (
        counts["classified_visible_native_count"] + counts["unclassified_count"]
    ):
        return "stateful_control_coverage_unproven"
    if counts["selected_or_filled_count"] > counts["discovered_count"]:
        return "stateful_control_coverage_unproven"
    if overflow != (counts["discovered_count"] > STATEFUL_CONTROL_COVERAGE_LIMIT):
        return "stateful_control_coverage_unproven"
    expected_complete = not overflow and counts["unclassified_count"] == 0
    if proof_complete is not expected_complete:
        return "stateful_control_coverage_unproven"
    if not proof_complete:
        return "stateful_control_unclassified"
    return None
