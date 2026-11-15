"""共同转折与地区独有转折复核。

在 regional_series 同一套可见版本与口径映射之上，对面板归一率识别共同转折：
每段回归含月历季节项与促销项，季节峰谷不参与转折判定；口径切换按当时映射
拆成不同经营身份，不跨身份连线；停业/缺测断开序列，不插补成连续经营；
共同日期由全面板汇总统计量一次选出，显著性用块自助法控制搜索偏差；
前后资料不足的身份留在证据不足一侧。
"""

import math
from collections import defaultdict

import numpy as np
from scipy import stats

from app.contracts import latest

GAP_STATUSES = {'closed', 'missing'}

DEFAULTS = {
    'min_segment_months': 24,
    'min_side_months': 4,
    'min_segments_per_date': 2,
    'resamples': 199,
    'block_months': 12,
    'seed': 20261115,
    'alpha': 0.05,
    'evidence_alpha': 0.1,
    'window_trim_months': 6,
    'max_windows': 4,
}

CAUSE_LABELS = {
    'missing': '缺测',
    'closed': '停业',
    'unmapped': '无有效口径映射',
    'ambiguous_mapping': '口径映射不唯一',
    'nonpositive_rate': '归一率非正，无法取对数',
}


def month_index(name):
    year, month = name.split('-')
    return int(year) * 12 + int(month) - 1


def month_name(index):
    return '%04d-%02d' % (index // 12, index % 12 + 1)


def _parse_month(value):
    if not isinstance(value, str):
        raise ValueError('月份须为 YYYY-MM 字符串')
    parts = value.split('-')
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit() \
            or not 1 <= int(parts[1]) <= 12 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError('月份须为 YYYY-MM 字符串')
    return month_index(value)


def _r(value, digits=6):
    return None if value is None else round(float(value), digits)


def _rp(value):
    """p 值保留 6 位小数，极小值用科学计数避免四舍五入成 0。"""
    if value is None:
        return None
    value = float(value)
    return round(value, 6) if value >= 1e-6 else float('%.2e' % value)


def _pct(log_shift):
    return 100.0 * (math.exp(log_shift) - 1.0)


def _native(value):
    if isinstance(value, dict):
        return {key: _native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_native(item) for item in value.tolist()]
    return value


def _bh(pvalues):
    """Benjamini-Hochberg 调整，控制逐段证据判定的错误发现。"""
    count = len(pvalues)
    adjusted = [1.0] * count
    previous = 1.0
    for rank, index in sorted(enumerate(sorted(range(count), key=lambda i: pvalues[i]), 1),
                              reverse=True):
        previous = min(previous, pvalues[index] * count / rank)
        adjusted[index] = previous
    return adjusted


def _block_resample(errors, replicates, block, rng):
    """循环块重抽样，按块长保留残差内部的相关与台阶形态。"""
    n = len(errors)
    blocks = int(math.ceil(n / block))
    starts = rng.integers(0, n, size=(replicates, blocks))
    offsets = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return errors[offsets.reshape(replicates, blocks * block)[:, :n]]


def _break_block(n, position):
    t = np.arange(n, dtype=float)
    step = (t >= position).astype(float)
    return np.column_stack([step, np.maximum(0.0, t - position)])


def _base_matrix(months, promotions):
    """截距、趋势、月历季节项与促销项；全零列（如窗口内无促销月）剔除。"""
    n = len(months)
    columns = [np.ones(n), np.arange(n, dtype=float)]
    for m in range(1, 12):
        columns.append(np.array([1.0 if idx % 12 == m else 0.0 for idx in months]))
    columns.append(np.array([1.0 if idx in promotions else 0.0 for idx in months]))
    matrix = np.column_stack(columns)
    keep = [i for i in range(matrix.shape[1])
            if i == 0 or np.linalg.norm(matrix[:, i]) > 0]
    return matrix[:, keep]


def _f_stats(Y, Q0, Q1s, df):
    """各候选断点位置的水平+趋势漂移联合 F 统计量；Y 可为 (B, n) 批量。"""
    Y = np.atleast_2d(np.asarray(Y, dtype=float))
    yy = np.einsum('bn,bn->b', Y, Y)
    ssr0 = yy - np.sum((Y @ Q0) ** 2, axis=1)
    out = {}
    for k, Q1 in Q1s.items():
        ssr1 = yy - np.sum((Y @ Q1) ** 2, axis=1)
        gain = np.maximum(ssr0 - ssr1, 0.0)
        # 相对下限，避免无噪声序列出现除零
        out[k] = gain / 2.0 * df / np.maximum(ssr1, 1e-12 * np.maximum(yy, 1e-300))
    return out


def build_panel(snapshot):
    """把可见活动映射为经营身份月序列：停业/缺测/口径不明断开，不跨缺口连线。"""
    activity = latest(snapshot['activity'], ['region_code', 'month'])
    versions = snapshot['region_versions']
    coverage = {(row['region_code'], row['month']): row.get('status')
                for row in snapshot.get('coverage', [])}
    cells = defaultdict(lambda: defaultdict(list))
    gaps = []
    for row in activity:
        region_code, month = row['region_code'], row['month']
        mappings = [v for v in versions if v['region_code'] == region_code
                    and v['valid_from'] <= month < v['valid_to']]
        status = coverage.get((region_code, month))
        cause = None
        if len(mappings) != 1:
            cause = 'unmapped' if not mappings else 'ambiguous_mapping'
        elif status in GAP_STATUSES:
            cause = status
        elif row['value'] is None:
            cause = 'missing'
        elif row['open_hours'] <= 0:
            cause = 'closed'
        if cause is None:
            rate = row['value'] / row['open_hours'] * mappings[0]['scale']
            if rate > 0:
                cells[mappings[0]['area_id']][month_index(month)].append((rate, region_code))
                continue
            cause = 'nonpositive_rate'
        gaps.append({'region_code': region_code, 'month': month, 'cause': cause})
    segments = []
    for area_id in sorted(cells):
        by_month = {}
        for idx, entries in cells[area_id].items():
            if len(entries) != 1:
                for _, region_code in entries:
                    gaps.append({'region_code': region_code, 'month': month_name(idx),
                                 'cause': 'ambiguous_mapping'})
                continue
            by_month[idx] = entries[0]
        run = None
        for idx in range(min(by_month), max(by_month) + 1):
            cell = by_month.get(idx)
            if cell is None:
                run = None
                continue
            if run is None:
                run = {'area_id': area_id, 'months': [], 'rates': [], 'region_codes': set()}
                segments.append(run)
            run['months'].append(idx)
            run['rates'].append(cell[0])
            run['region_codes'].add(cell[1])
    for seg in segments:
        seg['key'] = '%s|%s' % (seg['area_id'], month_name(seg['months'][0]))
        seg['region_codes'] = sorted(seg['region_codes'])
    gaps.sort(key=lambda gap: (gap['month'], gap['region_code']))
    return segments, gaps


def boundary_changes(versions):
    """同一 region_code 相邻有效区间换了经营身份，即为口径切换点。"""
    by_region = defaultdict(list)
    for version in versions:
        by_region[version['region_code']].append(version)
    changes = []
    for region_code in sorted(by_region):
        ordered = sorted(by_region[region_code], key=lambda v: v['valid_from'])
        for earlier, later in zip(ordered, ordered[1:]):
            if earlier['area_id'] != later['area_id']:
                changes.append({'region_code': region_code, 'month': later['valid_from'],
                                'from': earlier['area_id'], 'to': later['area_id']})
    return changes


def _prepare(seg, first, last, params, promotions):
    keep = [i for i, m in enumerate(seg['months']) if first <= m <= last]
    months = [seg['months'][i] for i in keep]
    rates = [seg['rates'][i] for i in keep]
    n = len(months)
    base = {'key': seg['key'], 'area_id': seg['area_id'], 'region_codes': seg['region_codes'],
            'months': months, 'rates': rates, 'n': n}
    if n < params['min_segment_months']:
        base.update(status='insufficient_data',
                    reason='窗口内连续营业不足 %d 个月' % params['min_segment_months'])
        return base
    y = np.log(np.asarray(rates, dtype=float))
    X0 = _base_matrix(months, promotions)
    df = n - (X0.shape[1] + 2)
    min_side = params['min_side_months']
    candidates = list(range(min_side, n - min_side + 1))
    if df < 4 or not candidates:
        base.update(status='insufficient_data', reason='序列长度不足以分离季节项与转折项')
        return base
    Q0, _ = np.linalg.qr(X0)
    Q1 = {}
    for k in candidates:
        X1 = np.column_stack([X0, _break_block(n, k)])
        Q1[k], _ = np.linalg.qr(X1)
    fit0 = Q0 @ (Q0.T @ y)
    base.update(status='evaluated', y=y, X0=X0, Q0=Q0, fit0=fit0, e0=y - fit0,
                candidates=candidates, Q1=Q1, df=df)
    return base


def _position_of(prepared, tau):
    try:
        k = prepared['months'].index(tau)
    except ValueError:
        return None
    return k if k in prepared['candidates'] else None


def _fit_at(prepared, k):
    """在指定断点位置估计水平/趋势漂移规模及标准误。"""
    X1 = np.column_stack([prepared['X0'], _break_block(prepared['n'], k)])
    beta, _, _, _ = np.linalg.lstsq(X1, prepared['y'], rcond=None)
    resid = prepared['y'] - X1 @ beta
    ssr1 = float(resid @ resid)
    df = prepared['df']
    cov = (ssr1 / df) * np.linalg.pinv(X1.T @ X1)
    lam, kap = float(beta[-2]), float(beta[-1])
    effect = lam + 6.0 * kap
    var_effect = float(cov[-2, -2] + 36.0 * cov[-1, -1] + 12.0 * cov[-2, -1])
    ssr0 = float(prepared['y'] @ prepared['y']
                 - np.sum((prepared['Q0'].T @ prepared['y']) ** 2))
    yy = float(prepared['y'] @ prepared['y'])
    f_stat = max(ssr0 - ssr1, 0.0) / 2.0 * df / max(ssr1, 1e-12 * max(yy, 1e-300))
    pre = [r for m, r in zip(prepared['months'], prepared['rates'])
           if m < prepared['months'][k]]
    return {'level_log': lam, 'level_se': math.sqrt(max(float(cov[-2, -2]), 0.0)),
            'trend_log': kap, 'trend_se': math.sqrt(max(float(cov[-1, -1]), 0.0)),
            'effect6_log': effect, 'effect6_se': math.sqrt(max(var_effect, 0.0)),
            'f': f_stat, 'p_value': float(1.0 - stats.f.cdf(f_stat, 2, df)),
            'tcrit': float(stats.t.ppf(0.975, df)),
            'baseline_rate': float(np.median(pre)) if pre else None}


def _shift_block(estimate, se, tcrit):
    return {'estimate': _r(_pct(estimate), 3),
            'lower': _r(_pct(estimate - tcrit * se), 3),
            'upper': _r(_pct(estimate + tcrit * se), 3)}


def _evidence(prepared_list, tau, params):
    """在共同（或指定）日期上逐段判定：支持/反向/无可检变化/证据不足。"""
    fits = {}
    rows = {}
    for p in prepared_list:
        row = {'area_id': p['area_id'], 'region_codes': p['region_codes']}
        if p['status'] != 'evaluated':
            rows[p['key']] = {**row, 'verdict': 'insufficient_data', 'reason': p['reason']}
            continue
        k = _position_of(p, tau)
        if k is None:
            rows[p['key']] = {**row, 'verdict': 'insufficient_data',
                              'reason': '该日期前后营业月数不足，无法估计漂移'}
            continue
        fits[p['key']] = (p, _fit_at(p, k))
        rows[p['key']] = row
    keys = list(fits)
    qvalues = _bh([fits[key][1]['p_value'] for key in keys]) if keys else []
    significant = [key for key, q in zip(keys, qvalues) if q <= params['evidence_alpha']]
    pool = significant or keys
    direction = None
    if pool:
        # 方向取显著段效应的中位数符号，避免个别近零标准误支配全体结论
        direction = float(np.sign(np.median([fits[key][1]['effect6_log'] for key in pool])))
    for key, q in zip(keys, qvalues):
        p, fit = fits[key]
        if q > params['evidence_alpha'] or not direction:
            verdict = 'no_detectable_shift'
        else:
            verdict = 'supports' if np.sign(fit['effect6_log']) == direction else 'opposes'
        rows[key] = {**rows[key], 'verdict': verdict,
                     'baseline_rate': _r(fit['baseline_rate'], 4),
                     'level_shift_pct': _shift_block(fit['level_log'], fit['level_se'], fit['tcrit']),
                     'trend_shift_pct_per_month': _shift_block(fit['trend_log'], fit['trend_se'], fit['tcrit']),
                     'effect_6m_pct': _shift_block(fit['effect6_log'], fit['effect6_se'], fit['tcrit']),
                     'p_value': _rp(fit['p_value']), 'q_value': _rp(q)}
    ordered = [rows[p['key']] for p in prepared_list]
    groups = {'supports': [], 'opposes': [], 'no_detectable_shift': [], 'insufficient_data': []}
    for row in ordered:
        groups[row['verdict']].append(row['area_id'])
    estimates = [fits[key][1] for key in keys]
    magnitude = None
    heterogeneity = None
    if estimates:
        effects = [_pct(fit['effect6_log']) for fit in estimates]
        levels = [_pct(fit['level_log']) for fit in estimates]
        array = np.asarray(effects)
        magnitude = {
            'effect_6m_pct': {'median': _r(np.median(array), 3),
                              'q1': _r(np.percentile(array, 25), 3),
                              'q3': _r(np.percentile(array, 75), 3),
                              'min': _r(array.min(), 3), 'max': _r(array.max(), 3)},
            'level_shift_pct_median': _r(np.median(levels), 3),
            'same_sign_share': _r(float(np.mean(np.sign(effects) == direction)), 3)
            if direction else None,
        }
        if len(estimates) >= 2:
            theta = np.asarray([fit['effect6_log'] for fit in estimates])
            w = 1.0 / np.maximum(np.asarray([fit['effect6_se'] for fit in estimates]), 1e-12) ** 2
            pooled = float(np.sum(w * theta) / np.sum(w))
            cochran_q = float(np.sum(w * (theta - pooled) ** 2))
            df = len(estimates) - 1
            p_het = float(1.0 - stats.chi2.cdf(cochran_q, df))
            heterogeneity = {'cochran_q': _r(cochran_q), 'df': df, 'p_value': _rp(p_het),
                             'verdict': 'heterogeneous' if p_het <= params['evidence_alpha']
                             else 'compatible_with_common_size'}
    return ordered, magnitude, heterogeneity, groups


def _scan_window(segments, window, params, promotions, rng):
    first, last = window
    prepared = [_prepare(seg, first, last, params, promotions) for seg in segments]
    evaluated = [p for p in prepared if p['status'] == 'evaluated']
    public = {'window': {'first': month_name(first), 'last': month_name(last)},
              'evaluated_segments': len(evaluated)}
    internals = {'prepared': prepared, 'evaluated': evaluated, 'tau_hat': None}
    tau_map = defaultdict(list)
    for i, p in enumerate(evaluated):
        for k in p['candidates']:
            tau_map[p['months'][k]].append((i, k))
    dates = sorted(tau for tau, lst in tau_map.items()
                   if len(lst) >= params['min_segments_per_date'])
    if not dates:
        public['common_break'] = None
        return public, internals
    f_obs = [_f_stats(p['y'], p['Q0'], p['Q1'], p['df']) for p in evaluated]
    s_obs = {tau: float(sum(f_obs[i][k][0] for i, k in tau_map[tau])) for tau in dates}
    tau_hat = max(dates, key=lambda tau: s_obs[tau])
    replicates = params['resamples']
    s_null = {tau: np.zeros(replicates) for tau in dates}
    max_f_null = []
    for i, p in enumerate(evaluated):
        Y = p['fit0'][None, :] + _block_resample(p['e0'], replicates,
                                                 params['block_months'], rng)
        f_boot = _f_stats(Y, p['Q0'], p['Q1'], p['df'])
        max_f_null.append(np.max(np.column_stack([f_boot[k] for k in p['candidates']]), axis=1))
        for k in p['candidates']:
            tau = p['months'][k]
            if tau in s_null:
                s_null[tau] += f_boot[k]
    null_matrix = np.column_stack([s_null[tau] for tau in dates])
    max_s_null = null_matrix.max(axis=1)
    p_common = (1.0 + float(np.sum(max_s_null >= s_obs[tau_hat]))) / (replicates + 1.0)
    own = []
    for i, p in enumerate(evaluated):
        k_best = max(p['candidates'], key=lambda k: f_obs[i][k][0])
        f_best = float(f_obs[i][k_best][0])
        p_own = (1.0 + float(np.sum(max_f_null[i] >= f_best))) / (replicates + 1.0)
        own.append({'prep': p, 'k': k_best, 'tau': p['months'][k_best],
                    'f': f_best, 'p_value': p_own})
    # 备选假设下重抽样：各段按自身最优断点模型生成，共同日期的可发生范围
    # 因而反映段间真实异质性，而不是强制所有段在同一日期变化。
    s_alt = {tau: np.zeros(replicates) for tau in dates}
    for item in own:
        p = item['prep']
        Q1 = p['Q1'][item['k']]
        fit1 = Q1 @ (Q1.T @ p['y'])
        errors = p['y'] - fit1
        Y = fit1[None, :] + _block_resample(errors, replicates,
                                            params['block_months'], rng)
        f_boot = _f_stats(Y, p['Q0'], p['Q1'], p['df'])
        for k in p['candidates']:
            tau = p['months'][k]
            if tau in s_alt:
                s_alt[tau] += f_boot[k]
    alt_matrix = np.column_stack([s_alt[tau] for tau in dates])
    boot_taus = [dates[j] for j in alt_matrix.argmax(axis=1)]
    lo = int(np.quantile(boot_taus, 0.1, method='nearest'))
    hi = int(np.quantile(boot_taus, 0.9, method='nearest'))
    share = float(np.mean([tau == tau_hat for tau in boot_taus]))
    ranked = sorted(dates, key=lambda tau: s_obs[tau], reverse=True)[:5]
    public['common_break'] = {
        'date': month_name(tau_hat),
        'statistic': _r(s_obs[tau_hat]),
        'p_value': _rp(p_common),
        'significant': bool(p_common <= params['alpha']),
        'plausible_range': {'first': month_name(lo), 'last': month_name(hi)},
        'replicate_share_at_date': _r(share),
        'contributing_segments': len(tau_map[tau_hat]),
        'coincides_with_promotion': tau_hat in promotions,
        'candidate_dates': [{'date': month_name(tau),
                             'statistic': _r(s_obs[tau]),
                             'contributing_segments': len(tau_map[tau])}
                            for tau in ranked],
    }
    internals.update(tau_hat=tau_hat, dates=dates, s_obs=s_obs, s_null=s_null,
                     tau_map=tau_map, own=own, range=(lo, hi))
    return public, internals


def _resolve_windows(options, lo, hi, params):
    if options.get('windows'):
        windows = []
        for item in list(options['windows'])[:params['max_windows']]:
            first, last = _parse_month(item['first']), _parse_month(item['last'])
            if last < first:
                raise ValueError('分析窗口起止无效')
            windows.append((first, last))
        return windows
    trim = params['window_trim_months']
    windows = [(lo, hi)]
    for candidate in ((lo + trim, hi), (lo, hi - trim)):
        if candidate[1] - candidate[0] + 1 >= params['min_segment_months'] \
                and candidate not in windows:
            windows.append(candidate)
    return windows


def _window_review(scan, params):
    public, internals = scan
    common = public['common_break']
    entry = {'window': public['window'],
             'evaluated_segments': public['evaluated_segments'],
             'date': common['date'] if common else None,
             'significant': common['significant'] if common else None,
             'p_value': common['p_value'] if common else None,
             'plausible_range': common['plausible_range'] if common else None,
             'supports': []}
    if internals['tau_hat'] is not None:
        _, _, _, groups = _evidence(internals['prepared'], internals['tau_hat'], params)
        entry['supports'] = groups['supports']
    return entry


def common_break_scan(snapshot, options):
    """识别面板共同转折与地区独有转折，输出证据、规模、异质性与窗口稳健性。"""
    options = dict(options or {})
    params = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in options:
            params[key] = options[key]
    for key in ('min_segment_months', 'min_side_months', 'min_segments_per_date',
                'resamples', 'block_months', 'seed', 'window_trim_months', 'max_windows'):
        params[key] = int(params[key])
    for key in ('alpha', 'evidence_alpha'):
        params[key] = float(params[key])
    params['resamples'] = max(1, params['resamples'])
    hypothesized_idx = None
    if options.get('hypothesized_month') is not None:
        hypothesized_idx = _parse_month(options['hypothesized_month'])
    segments, gaps = build_panel(snapshot)
    changes = boundary_changes(snapshot['region_versions'])
    promotions = {month_index(row['month']) for row in snapshot.get('calendar', [])
                  if row.get('promotion')}
    all_months = [m for seg in segments for m in seg['months']]
    windows = _resolve_windows(options, min(all_months), max(all_months), params) \
        if all_months else []
    rng = np.random.default_rng(params['seed'])
    scans = [_scan_window(segments, window, params, promotions, rng) for window in windows]

    primary_public, primary = scans[0] if scans else (
        {'window': None, 'evaluated_segments': 0, 'common_break': None},
        {'prepared': [], 'evaluated': [], 'tau_hat': None})
    common = primary_public['common_break']

    hypothesized_block = None
    evaluation_tau = primary['tau_hat']
    evaluation_basis = 'scan_estimate' if evaluation_tau is not None else None
    evaluation_note = None
    if hypothesized_idx is not None:
        if primary.get('dates') and hypothesized_idx in primary['s_obs']:
            observed = primary['s_obs'][hypothesized_idx]
            null = primary['s_null'][hypothesized_idx]
            p_fixed = (1.0 + float(np.sum(null >= observed))) / (params['resamples'] + 1.0)
            hypothesized_block = {
                'date': month_name(hypothesized_idx), 'evaluable': True,
                'statistic': _r(observed), 'p_value': _rp(p_fixed),
                'significant': bool(p_fixed <= params['alpha']),
                'contributing_segments': len(primary['tau_map'][hypothesized_idx]),
                'coincides_with_promotion': hypothesized_idx in promotions}
            evaluation_tau = hypothesized_idx
            evaluation_basis = 'hypothesized'
        else:
            hypothesized_block = {
                'date': month_name(hypothesized_idx), 'evaluable': False,
                'reason': '该月不在可评估的共同日期范围内（两侧样本或可比地区数不足）'}
            evaluation_note = '指定月份不可评估，证据与规模按扫描选出的日期给出'

    evidence, magnitude, heterogeneity, groups = [], None, None, {
        'supports': [], 'opposes': [], 'no_detectable_shift': [], 'insufficient_data': []}
    if evaluation_tau is not None:
        evidence, magnitude, heterogeneity, groups = _evidence(
            primary['prepared'], evaluation_tau, params)

    region_specific = []
    if primary.get('own'):
        qvalues = _bh([item['p_value'] for item in primary['own']])
        for item, q in zip(primary['own'], qvalues):
            # 与共同日期相邻（±1 月）的段内断点已计入共同结构证据，
            # 不重复列为地区独有变化；远离共同日期的才列出
            near_common = common and abs(item['tau'] - primary['tau_hat']) <= 1
            if q > params['evidence_alpha'] or near_common:
                continue
            fit = _fit_at(item['prep'], item['k'])
            region_specific.append({
                'area_id': item['prep']['area_id'],
                'region_codes': item['prep']['region_codes'],
                'date': month_name(item['tau']),
                'p_value': _rp(item['p_value']), 'q_value': _rp(q),
                'level_shift_pct': _r(_pct(fit['level_log']), 3),
                'trend_shift_pct_per_month': _r(_pct(fit['trend_log']), 3),
                'effect_6m_pct': _r(_pct(fit['effect6_log']), 3),
                'coincides_with_promotion': item['tau'] in promotions})

    reviews = [_window_review(scan, params) for scan in scans]
    if common is None:
        verdict = 'insufficient_data'
    elif not common['significant']:
        verdict = 'no_common_break'
    elif groups['opposes'] or (heterogeneity and heterogeneity['verdict'] == 'heterogeneous'):
        # 有共同日期的统计证据，但方向或规模不一致，不能当作全国同一变化
        verdict = 'common_break_heterogeneous'
    else:
        verdict = 'common_break_confirmed'
    robust = None
    if len(reviews) > 1:
        def consistent(entry):
            if common is None:
                return entry['date'] is None
            if entry['significant'] != common['significant']:
                return False
            if not common['significant']:
                return True
            return abs(month_index(entry['date']) - month_index(common['date'])) <= 1
        robust = all(consistent(entry) for entry in reviews[1:])

    result = {
        'analysis': 'common_break_scan',
        'window': primary_public['window'],
        'parameters': {**params,
                       'hypothesized_month': options.get('hypothesized_month'),
                       'windows': [{'first': month_name(f), 'last': month_name(l)}
                                   for f, l in windows]},
        'panel': {'segments': [
            {'area_id': seg['area_id'], 'region_codes': seg['region_codes'],
             'first': month_name(seg['months'][0]), 'last': month_name(seg['months'][-1]),
             'months': len(seg['months']),
             'status': next((p['status'] for p in primary['prepared']
                             if p['key'] == seg['key']), 'insufficient_data'),
             'reason': next((p.get('reason') for p in primary['prepared']
                             if p['key'] == seg['key']), None)}
            for seg in segments]},
        'common_break': common,
        'hypothesized_month': hypothesized_block,
        'evaluation_date': month_name(evaluation_tau) if evaluation_tau is not None else None,
        'evaluation_basis': evaluation_basis,
        'evaluation_note': evaluation_note,
        'evidence': evidence,
        'magnitude': magnitude,
        'heterogeneity': heterogeneity,
        'region_specific_breaks': region_specific,
        'window_robustness': reviews,
        'robust_to_windows': robust,
        'guards': {
            'rate_basis': '按营业机会归一率（value/open_hours）分析，新增网点造成的总量变化不直接构成转折',
            'seasonality': '每段回归含月历季节项与促销项，季节峰谷与单月促销不作为转折证据',
            'exposure_calendar': 'open_hours 的非日历周期不单独建模，由块自助法在显著性与区间推断中吸收，周期峰谷不直接判为结构变化',
            'promotion_months': sorted(month_name(idx) for idx in promotions),
            'boundary_changes': [{**change,
                                  'treatment': '按当时映射拆为不同经营身份，不跨身份连线'}
                                 for change in changes],
            'gaps_not_bridged': [{**gap, 'label': CAUSE_LABELS.get(gap['cause'], gap['cause'])}
                                 for gap in gaps],
            'resampling': {'method': 'circular_block_bootstrap',
                           'block_months': params['block_months'],
                           'replicates': params['resamples'], 'seed': params['seed']},
            'min_segment_months': params['min_segment_months'],
            'min_side_months': params['min_side_months'],
            'limitations': [
                '残差块重抽样仅按块长保留相关形态，更长的相关结构会低估不确定度',
                '样本不足的段按证据不足处理，不等于确认无变化',
                '旧分析按请求 as_of 时刻的可见版本与口径映射重现，追补与口径调整不回写历史结论'],
        },
        'headline': {'verdict': verdict,
                     'date': common['date'] if common else None,
                     **groups},
    }
    return _native(result)
