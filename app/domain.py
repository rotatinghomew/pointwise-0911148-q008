from collections import Counter
from pathlib import Path

from app.breaks import common_break_scan
from app.contracts import latest, load
from app.repository import digest

TABLES = ['activity', 'region_versions', 'calendar', 'coverage']

def regional_series(snapshot, options):
    activity = latest(snapshot['activity'], ['region_code', 'month'])
    series = []
    for row in activity:
        mappings = [version for version in snapshot['region_versions'] if version['region_code'] == row['region_code']
                    and version['valid_from'] <= row['month'] < version['valid_to']]
        mapping = mappings[0] if len(mappings) == 1 else None
        rate = row['value'] / row['open_hours'] * mapping['scale'] if mapping and row['value'] is not None and row['open_hours'] > 0 else None
        series.append({'region_code': row['region_code'], 'month': row['month'],
                       'area_id': mapping['area_id'] if mapping else None, 'rate': rate})
    rates = [row['rate'] for row in series if row['rate'] is not None]
    return {'regions': len({row['region_code'] for row in activity}), 'months': len({row['month'] for row in activity}),
            'valid_region_months': len(rates), 'missing_values': sum(row['value'] is None for row in activity),
            'mean_rate': sum(rates) / len(rates) if rates else None,
            'boundary_versions': len(snapshot['region_versions']), 'series': series}

def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')

OPERATIONS = {'regional_series': regional_series, 'common_break_scan': common_break_scan}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
