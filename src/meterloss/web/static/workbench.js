const byId = (id) => document.getElementById(id);
const stageLinks = [...document.querySelectorAll('[data-stage]')];
let active;
let selection = null;
let selectedRow = null;
const formatter = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 3});

function text(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number') return formatter.format(value);
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (Array.isArray(value)) return value.map(text).join('、');
    if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key} ${text(item)}`).join(' · ');
    return String(value);
}

function named(tag, value, css) {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = text(value);
    if (css) element.className = css;
    return element;
}

function tableFor(records, keys, labels, select, caption) {
    const table = named('table');
    if (caption) table.createCaption().textContent = caption;
    const header = table.createTHead().insertRow();
    keys.forEach((key) => {
        const column = named('th', labels?.[key] ?? key);
        column.scope = 'col';
        header.append(column);
    });
    const rows = table.createTBody();
    records.forEach((record) => {
        const line = rows.insertRow();
        keys.forEach((key, index) => {
            const cell = line.insertCell();
            if (index === 0 && select) {
                const button = named('button', record[key], 'record-link');
                button.type = 'button';
                button.addEventListener('click', () => select(record));
                cell.append(button);
            } else {
                cell.textContent = text(record[key]);
            }
        });
    });
    return table;
}

function renderTable(target, records, labels, select) {
    target.replaceChildren();
    if (records.length === 0) {
        target.append(named('p', '当前条件下没有记录', 'empty'));
        return;
    }
    if (labels) {
        target.append(tableFor(records, Object.keys(labels), labels, select));
        return;
    }
    const groups = new Map();
    for (const record of records) {
        const fields = Object.keys(record);
        if (!fields.length) continue;
        const signature = JSON.stringify([...fields].sort());
        if (!groups.has(signature)) groups.set(signature, {fields, records: []});
        groups.get(signature).records.push(record);
    }
    for (const group of groups.values()) {
        const caption = groups.size > 1 ? `${group.records.length} 条证据` : null;
        target.append(tableFor(group.records, group.fields, null, select, caption));
    }
}

function renderMetrics(metrics) {
    const entries = Array.isArray(metrics) ? metrics.map((item) => [item.label, item.value]) : Object.entries(metrics);
    const items = entries.map(([label, value]) => {
        const item = named('div', undefined, 'kpi');
        item.append(named('dt', label), named('dd', value));
        return item;
    });
    byId('metrics').replaceChildren(...items);
}

function renderRows() {
    const query = byId('row-filter').value.toLocaleLowerCase().trim();
    const visible = active.rows.filter((record) => Object.values(record).some((entry) => text(entry).toLocaleLowerCase().includes(query)) || !query);
    byId('row-count').textContent = `${visible.length} / ${active.rows.length} 条`;
    renderTable(byId('rows'), visible, active.columns, openRecord);
}

function openRecord(record) {
    selectedRow = record;
    selection = 'record';
    renderEvidence();
    byId('evidence-heading').focus({preventScroll:true});
    byId('evidence').scrollIntoView({block:'nearest'});
}

function renderEvidence() {
    const selected = typeof selection === 'number' ? active?.stages[selection] : null;
    stageLinks.forEach((link, index) => {
        link.setAttribute('aria-pressed', String(index === selection));
    });
    if (selection === 'record' && selectedRow) {
        byId('evidence-heading').textContent = '所选记录';
        byId('evidence-summary').textContent = '';
        const list = named('dl', undefined, 'record-details');
        Object.entries(selectedRow).forEach(([key, value]) => {
            list.append(named('dt', active.columns[key] ?? key), named('dd', value));
        });
        byId('stage-records').replaceChildren(list);
        return;
    }
    if (!selected) return;
    byId('evidence-heading').textContent = selected.title;
    byId('evidence-summary').textContent = selected.summary;
    renderTable(byId('stage-records'), selected.records || [], selected.columns);
    if (selected.errors?.length) {
        const issues = named('details');
        issues.append(named('summary', `隔离 ${selected.errors.length} 条记录`));
        const records = named('div');
        renderTable(records, selected.errors);
        issues.append(records);
        byId('stage-records').append(issues);
    }
}

const LINE_COLUMNS = {
    cohort: '用水簇', usage: '营业类型', previous_line: '原预警线', adopted_line: '下一班预警线',
    direction: '处置', confirmed_cases: '确认', excluded_cases: '排除', unjudged_cases: '缺测不判',
    dispatches_before: '原占用', dispatches_after: '新占用', reason: '改线依据',
};
const TICKET_COLUMNS = {
    ticket: '工单号', meter: '水表', sector: '分区', task: '任务', crew: '巡检组', state: '状态',
    from_minute: '开始分', to_minute: '结束分', alert_line: '预警线', line_version: '预警线版本',
};

function renderLines(snapshot) {
    const rows = snapshot.alert_lines || [];
    const changed = rows.filter((row) => row.changed).length;
    byId('lines-summary').textContent = rows.length
        ? `${changed} 个簇改线 · 预警线版本 ${snapshot.line_version || '—'}`
        : '本班无回放结果';
    renderTable(byId('alert-lines'), rows, LINE_COLUMNS);
}

function renderTickets(snapshot) {
    const rows = snapshot.dispatch_tickets || [];
    const scheduled = rows.filter((row) => row.state === '已派单').length;
    byId('tickets-summary').textContent = `${rows.length} 张 · 已派单 ${scheduled} · 本批新增 ${snapshot.metrics?.['新增工单'] ?? 0}`;
    renderTable(byId('tickets'), rows, TICKET_COLUMNS);
}

function render(snapshot) {
    active = snapshot;
    selectedRow = null;
    if (typeof selection !== 'number') selection = snapshot.stages.length - 1;
    byId('run-id').textContent = snapshot.run_id;
    byId('export').disabled = false;
    renderMetrics(snapshot.metrics);
    renderRows();
    renderLines(snapshot);
    renderTickets(snapshot);
    renderEvidence();
    const chart = byId('chart');
    chart.replaceChildren();
    drawProjectChart(chart, snapshot.series.filter(({value}) => Number.isFinite(value)).slice(0, 14), snapshot.stages);
}

async function getResult(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw new Error(error?.message || `读取失败（${response.status}）`);
    }
    return response.json();
}

function announce(message, error = false) {
    byId('status').textContent = message;
    byId('status').classList.toggle('error', error);
}

stageLinks.forEach((link, index) => {
    link.addEventListener('click', () => {
        if (!active) return;
        selection = index;
        renderEvidence();
        byId('evidence').scrollIntoView({block:'nearest'});
        byId('evidence-heading').focus({preventScroll:true});
    });
});
byId('row-filter').addEventListener('input', () => { if (active) renderRows(); });
byId('scenario').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = byId('submit');
    const capacity = byId('capacity').valueAsNumber;
    button.disabled = true;
    announce('正在计算');
    try {
        render(await getResult('/api/scenario', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({capacity})
        }));
        announce('方案已更新');
    } catch (error) {
        announce(error.message, true);
    } finally {
        button.disabled = false;
    }
});
byId('export').addEventListener('click', () => {
    if (!active) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(active, null, 2)], {type:'application/json'}));
    const link = named('a');
    link.href = url;
    link.download = `${document.body.dataset.project}-${active.run_id}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
});
byId('print')?.addEventListener('click', () => window.print());

(async () => {
    try {
        render(await getResult('/api/overview'));
        announce('样例已就绪');
        byId('submit').disabled = false;
    } catch (error) {
        announce(error.message, true);
    }
})();
