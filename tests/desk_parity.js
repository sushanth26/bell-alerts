// Runs the desk's own provenDay (pulled from core.js / desk.js source) on a JSON dump. Usage: node desk_parity.js <deskDir> <dump.json>
const fs = require('fs'), path = require('path');
const [deskDir, dumpPath] = process.argv.slice(2);
const src = fs.readFileSync(path.join(deskDir, 'core.js'), 'utf8') + '\n' + fs.readFileSync(path.join(deskDir, 'desk.js'), 'utf8');
function grab(name) {
  let i = src.search(new RegExp(`\\n(async )?function ${name}\\(`));
  if (i >= 0) { let j = src.indexOf('{', src.indexOf(')', i)), d = 0; for (; j < src.length; j++) { if (src[j] === '{') d++; else if (src[j] === '}' && --d === 0) break; } return src.slice(i, j + 1); }
  i = src.search(new RegExp(`\\nconst ${name} =`)); if (i < 0) throw new Error('missing ' + name);
  return src.slice(i, src.indexOf('\n', i + 1));
}
const names = ['ETF', 'et', 'etDateOf', 'finished', 'finishedDaily', 'emaArr', 'step', 'cloud', 'atr14', 'provenStop', 'EXT', 'MIN_ATR_PCT', 'lowVol', 'lowVolText', 'extCheck', 'walkLot', 'GRADE_OK', 'spyCtx', 'relv5', 'provenDay'];
const S = { bars: {}, risk: 100 };
const run = new Function('S', names.map(grab).join('\n') + '\nreturn provenDay;');
const provenDay = run(S);
const dump = JSON.parse(fs.readFileSync(dumpPath, 'utf8'));
const J = a => (a || []).map(b => ({ t: b[0] * 1000, o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] }));
const out = [];
for (const [date, day] of Object.entries(dump)) {
  const now = day.now * 1000;
  const spyCtx = new Function('S', names.map(grab).join('\n') + '\nreturn spyCtx;')(S);
  const X = spyCtx(J(day.spy5), now, date);
  for (const [sym, d] of Object.entries(day.syms)) {
    S.bars[sym] = { '10m': { list: J(d.m10) }, '1h': { list: J(d.h1) }, '4h': { list: J(d.h4) }, D: { list: d.D.map(b => ({ d: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] })) } };
    for (const r of provenDay(sym, J(d.b5), X, now, date).rows) out.push({ date, sym, t: r.t / 1000, g: r.g, entry: r.entry, stop: r.stop, add: !!r.add, res: r.res, r: r.r });
  }
}
console.log(JSON.stringify(out));
