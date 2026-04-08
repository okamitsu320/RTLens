#!/usr/bin/env node
'use strict';

const fs = require('fs');
const ELK = require('./node_modules/elkjs/lib/elk.bundled.js');

async function main() {
  const input = fs.readFileSync(0, 'utf8');
  const graph = JSON.parse(input);
  const elk = new ELK();
  const result = await elk.layout(graph);
  process.stdout.write(JSON.stringify(result));
}

main().catch((err) => {
  const msg = err && err.stack ? err.stack : String(err);
  process.stderr.write(msg + '\n');
  process.exit(1);
});

