/**
 * Bot-metrics Google Sheets receiver.
 * Установка: вставить в Apps Script таблицы, развернуть как Web App
 * (Execute as: Me; Access: Anyone). URL вставить в .env как SHEETS_WEBHOOK_URL.
 */

const LEADS_SHEET = 'Лиды';
const DASH_SHEET = 'Дашборд';

const HEADERS = [
  'ID', 'Создан', 'Обновлён', 'Источник', 'Имя', 'Логин',
  'Запрос', 'Этап', 'Заметки', 'Причина отвала'
];

const STAGE_TITLES = {
  'lead_new':       'Заявка',
  'qualified':      'Квал',
  'breakdown_sent': 'Разбор',
  'agreed':         'Согласие',
  'paid':           'Оплата',
  'consulted':      'Консультация',
  'package_bought': 'Пакет',
  'lost':           'Отвал',
};

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ensureLeadsSheet_(ss);

    if (body.action === 'replace_all') {
      replaceAll_(sheet, body.leads || []);
    } else if (body.action === 'upsert') {
      upsert_(sheet, body.lead);
    }
    ensureDashboard_(ss);
    return ok_('ok');
  } catch (err) {
    return ok_('error: ' + err.message);
  }
}

function ok_(text) {
  return ContentService.createTextOutput(text).setMimeType(ContentService.MimeType.TEXT);
}

function ensureLeadsSheet_(ss) {
  let sh = ss.getSheetByName(LEADS_SHEET);
  if (!sh) {
    sh = ss.insertSheet(LEADS_SHEET, 0);
    sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold').setBackground('#1a73e8').setFontColor('white');
    sh.setFrozenRows(1);
    sh.setColumnWidth(1, 50);   // ID
    sh.setColumnWidth(2, 130);  // Создан
    sh.setColumnWidth(3, 130);  // Обновлён
    sh.setColumnWidth(4, 110);  // Источник
    sh.setColumnWidth(5, 140);  // Имя
    sh.setColumnWidth(6, 130);  // Логин
    sh.setColumnWidth(7, 380);  // Запрос
    sh.setColumnWidth(8, 130);  // Этап
    sh.setColumnWidth(9, 240);  // Заметки
    sh.setColumnWidth(10, 200); // Причина отвала
    applyStageFormatting_(sh);
  }
  return sh;
}

function applyStageFormatting_(sh) {
  const range = sh.getRange('H2:H1000');
  const rules = sh.getConditionalFormatRules() || [];
  const colors = {
    'Заявка':       '#e8f0fe',
    'Квал':         '#d2e3fc',
    'Разбор':       '#fef7e0',
    'Согласие':     '#fce8b2',
    'Оплата':       '#ceead6',
    'Консультация': '#a8dab5',
    'Пакет':        '#34a853',
    'Отвал':        '#fad2cf',
  };
  Object.keys(colors).forEach(function(stage) {
    const rule = SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo(stage)
      .setBackground(colors[stage])
      .setRanges([range])
      .build();
    rules.push(rule);
  });
  sh.setConditionalFormatRules(rules);
}

function leadRow_(lead) {
  return [
    lead.id,
    lead.created_at,
    lead.updated_at,
    lead.source_title || lead.source,
    lead.name || '',
    lead.username || '',
    lead.request || '',
    STAGE_TITLES[lead.stage] || lead.stage,
    lead.notes || '',
    lead.lost_reason || '',
  ];
}

function replaceAll_(sh, leads) {
  const lastRow = sh.getLastRow();
  if (lastRow > 1) {
    sh.getRange(2, 1, lastRow - 1, HEADERS.length).clearContent();
  }
  if (!leads.length) return;
  const rows = leads.map(leadRow_);
  sh.getRange(2, 1, rows.length, HEADERS.length).setValues(rows);
}

function upsert_(sh, lead) {
  const lastRow = sh.getLastRow();
  let targetRow = -1;
  if (lastRow > 1) {
    const ids = sh.getRange(2, 1, lastRow - 1, 1).getValues();
    for (let i = 0; i < ids.length; i++) {
      if (Number(ids[i][0]) === Number(lead.id)) {
        targetRow = i + 2;
        break;
      }
    }
  }
  if (targetRow === -1) {
    targetRow = lastRow + 1;
  }
  sh.getRange(targetRow, 1, 1, HEADERS.length).setValues([leadRow_(lead)]);
}

function ensureDashboard_(ss) {
  let sh = ss.getSheetByName(DASH_SHEET);
  if (sh) return;
  sh = ss.insertSheet(DASH_SHEET, 1);
  sh.setHiddenGridlines(true);

  // ---------- Header ----------
  sh.getRange('A1').setValue('📊 Дашборд').setFontSize(22).setFontWeight('bold');
  sh.getRange('A2').setValue('Все цифры считаются автоматически по листу «Лиды».').setFontColor('#5f6368');

  // ---------- Big numbers ----------
  const bigBoxes = [
    {row: 4, col: 1, label: 'Всего лидов',     formula: '=COUNTA(Лиды!A2:A)'},
    {row: 4, col: 3, label: 'Активных',        formula: '=COUNTIFS(Лиды!H2:H, "<>Отвал", Лиды!H2:H, "<>Пакет", Лиды!H2:H, "<>")'},
    {row: 4, col: 5, label: 'Оплат',           formula: '=COUNTIF(Лиды!H2:H, "Оплата") + COUNTIF(Лиды!H2:H, "Консультация") + COUNTIF(Лиды!H2:H, "Пакет")'},
    {row: 4, col: 7, label: 'Пакетов куплено', formula: '=COUNTIF(Лиды!H2:H, "Пакет")'},
  ];
  bigBoxes.forEach(function(b) {
    sh.getRange(b.row, b.col).setValue(b.label).setFontColor('#5f6368').setFontSize(11);
    sh.getRange(b.row + 1, b.col).setFormula(b.formula).setFontSize(28).setFontWeight('bold');
    sh.getRange(b.row, b.col, 2, 1).setBackground('#f1f3f4');
  });

  // ---------- Funnel ----------
  sh.getRange('A8').setValue('Воронка (текущее состояние)').setFontSize(14).setFontWeight('bold');
  const funnelStages = ['Заявка', 'Квал', 'Разбор', 'Согласие', 'Оплата', 'Консультация', 'Пакет'];
  sh.getRange('A9').setValue('Этап').setFontWeight('bold').setBackground('#1a73e8').setFontColor('white');
  sh.getRange('B9').setValue('Лидов').setFontWeight('bold').setBackground('#1a73e8').setFontColor('white');
  funnelStages.forEach(function(stage, i) {
    sh.getRange(10 + i, 1).setValue(stage);
    sh.getRange(10 + i, 2).setFormula('=COUNTIF(Лиды!H2:H, "' + stage + '")');
  });
  sh.getRange('A17').setValue('Отвалилось').setFontWeight('bold');
  sh.getRange('B17').setFormula('=COUNTIF(Лиды!H2:H, "Отвал")').setFontWeight('bold');

  // Build a column chart for the funnel
  const funnelRange = sh.getRange('A9:B16');
  const chart = sh.newChart()
    .setChartType(Charts.ChartType.BAR)
    .addRange(funnelRange)
    .setOption('title', 'Воронка')
    .setOption('legend', {position: 'none'})
    .setOption('colors', ['#1a73e8'])
    .setOption('width', 600)
    .setOption('height', 320)
    .setPosition(8, 4, 0, 0)
    .build();
  sh.insertChart(chart);

  // ---------- Channels ----------
  sh.getRange('A20').setValue('По источникам').setFontSize(14).setFontWeight('bold');
  sh.getRange('A21:E21').setValues([['Источник', 'Лидов', 'Квал+', 'Оплат', 'Конверсия в оплату']])
    .setFontWeight('bold').setBackground('#1a73e8').setFontColor('white');
  const channels = ['Instagram', 'YouTube', 'Telegram-канал'];
  channels.forEach(function(ch, i) {
    const r = 22 + i;
    sh.getRange(r, 1).setValue(ch);
    sh.getRange(r, 2).setFormula('=COUNTIF(Лиды!D2:D, "' + ch + '")');
    sh.getRange(r, 3).setFormula('=COUNTIFS(Лиды!D2:D, "' + ch + '", Лиды!H2:H, "<>Заявка", Лиды!H2:H, "<>Отвал", Лиды!H2:H, "<>")');
    sh.getRange(r, 4).setFormula(
      '=COUNTIFS(Лиды!D2:D, "' + ch + '", Лиды!H2:H, "Оплата")'
      + '+COUNTIFS(Лиды!D2:D, "' + ch + '", Лиды!H2:H, "Консультация")'
      + '+COUNTIFS(Лиды!D2:D, "' + ch + '", Лиды!H2:H, "Пакет")'
    );
    sh.getRange(r, 5).setFormula('=IFERROR(D' + r + '/B' + r + ', 0)').setNumberFormat('0%');
  });

  const channelRange = sh.getRange('A21:B24');
  const channelChart = sh.newChart()
    .setChartType(Charts.ChartType.PIE)
    .addRange(channelRange)
    .setOption('title', 'Лиды по источникам')
    .setOption('width', 480)
    .setOption('height', 320)
    .setPosition(20, 7, 0, 0)
    .build();
  sh.insertChart(channelChart);

  sh.setColumnWidth(1, 160);
  sh.setColumnWidths(2, 4, 130);
}
