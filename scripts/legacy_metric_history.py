"""Map verified eLibrary annual observations onto the site's aligned series."""
import copy
import math


SERIES = {
    'risc_publications': 'Число публикаций в РИНЦ',
    'core_publications': 'Число публикаций в ядре РИНЦ',
    'risc_citations': 'Число цитирований в РИНЦ',
    'core_citations': 'Число цитирований из ядра РИНЦ',
    'h_index_risc': 'Индекс Хирша в РИНЦ',
    'h_index_core': 'Индекс Хирша по ядру РИНЦ',
}


def update_annual_history(previous, yearly_metrics):
    """Caller must verify source freshness; missing observations retain history."""
    result = copy.deepcopy(previous)
    observed = {}
    for key, label in SERIES.items():
        values = (yearly_metrics or {}).get(label) or {}
        if not isinstance(values, dict):
            continue
        valid = {int(year): value for year, value in values.items()
                 if str(year).isdigit() and len(str(year)) == 4
                 and isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0}
        if valid:
            observed[key] = valid
    if not observed:
        return result
    old_years = previous.get('years') or []
    years = sorted({int(year) for year in old_years if str(year).isdigit()}
                   | {year for values in observed.values() for year in values}, reverse=True)
    aligned_keys = {key for key, values in previous.items()
                    if key != 'years' and isinstance(values, list) and len(values) == len(old_years)}
    for key in aligned_keys | set(observed):
        values = {int(year): value for year, value in zip(old_years, previous.get(key) or [])
                  if str(year).isdigit()}
        values.update(observed.get(key, {}))
        result[key] = [values.get(year) for year in years]
    result['years'] = years
    return result
