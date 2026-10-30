// Subconjunto do `assert` do chai e dos `__helpers` do freeCodeCamp para o Node do Jobe.
// Corre DENTRO de um contexto `vm` novo, antes do código do aluno (que corre no mesmo contexto, sem
// `require` nem `process`): fica tudo num fecho, por isso o aluno não vê nem apanha estes nomes, e
// as funções dos testes criam-se antes do código do aluno. Só tem os delimitadores do Twig se o
// teste os apanhar: não pode ter nenhum.
// Só o que os desafios do freeCodeCamp usam; um teste que use outra coisa fica «não mapeado».
globalThis.__ariadne = (() => {
  'use strict';
  const O = Object;
  const keysOf = O.keys;
  const stringify = JSON.stringify;
  const toTag = O.prototype.toString;
  const hasOwn = O.prototype.hasOwnProperty;
  const isArray = Array.isArray;
  const NumberIsNaN = Number.isNaN;
  const NumberIsFinite = Number.isFinite;
  const AsyncFunction = (async () => {}).constructor;
  const regexTest = RegExp.prototype.test;
  const getTime = Date.prototype.getTime;
  const kind = (value) => toTag.call(value).slice(8, -1).toLowerCase();

  // Um `console` mudo: o que o aluno escreve não entra nos resultados.
  const quiet = () => {};
  globalThis.console = { log: quiet, info: quiet, warn: quiet, error: quiet, debug: quiet, table: quiet };

  function show(value, depth, seen) {
    depth = depth === undefined ? 4 : depth;
    seen = seen || [];
    if (typeof value === 'string') return stringify(value);
    if (typeof value === 'function') return '[Function' + (value.name ? ': ' + value.name : '') + ']';
    if (typeof value === 'bigint') return String(value) + 'n';
    if (typeof value === 'symbol') return String(value);
    if (value === null || typeof value !== 'object') return String(value);
    for (let i = 0; i < seen.length; i++) if (seen[i] === value) return '[Circular]';
    if (depth < 0) return isArray(value) ? '[Array]' : '[Object]';
    const nested = seen.concat([value]);
    const type = kind(value);
    if (type === 'array') {
      let text = '[';
      for (let i = 0; i < value.length; i++) text += (i ? ', ' : ' ') + show(value[i], depth - 1, nested);
      return text + (value.length ? ' ]' : ']');
    }
    if (type === 'map' || type === 'set') {
      let text = type === 'map' ? 'Map {' : 'Set {';
      let first = true;
      value.forEach((item, key) => {
        text += (first ? ' ' : ', ') + (type === 'map' ? show(key, depth - 1, nested) + ' => ' : '') + show(item, depth - 1, nested);
        first = false;
      });
      return text + (first ? '}' : ' }');
    }
    if (type === 'date') return 'Date ' + String(value);
    if (type === 'regexp') return String(value);
    if (type === 'error') return String(value);
    const keys = keysOf(value);
    let text = '{';
    for (let i = 0; i < keys.length; i++) text += (i ? ', ' : ' ') + keys[i] + ': ' + show(value[keys[i]], depth - 1, nested);
    return text + (keys.length ? ' }' : '}');
  }

  class AssertionError extends Error {
    constructor(message) {
      super(message);
      this.name = 'AssertionError';
    }
  }
  const fail = (message) => {
    throw new AssertionError(message);
  };
  const check = (ok, message, custom) => {
    if (!ok) fail(custom || message);
  };

  function deepEqual(a, b, seen) {
    if (a === b) return a !== 0 || 1 / a === 1 / b;
    if (a !== a && b !== b) return true; // NaN
    if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
    if (kind(a) !== kind(b)) return false;
    seen = seen || [];
    for (let i = 0; i < seen.length; i++) if (seen[i][0] === a && seen[i][1] === b) return true;
    seen.push([a, b]);
    const type = kind(a);
    if (type === 'date') return getTime.call(a) === getTime.call(b) || (a !== a && b !== b);
    if (type === 'regexp') return String(a) === String(b);
    if (type === 'map') {
      if (a.size !== b.size) return false;
      let same = true;
      a.forEach((value, key) => {
        if (!b.has(key) || !deepEqual(value, b.get(key), seen)) same = false;
      });
      return same;
    }
    if (type === 'set') {
      if (a.size !== b.size) return false;
      const rest = [];
      b.forEach((item) => rest.push(item));
      let same = true;
      a.forEach((item) => {
        let found = -1;
        for (let i = 0; i < rest.length; i++) {
          if (deepEqual(item, rest[i], seen)) {
            found = i;
            break;
          }
        }
        if (found < 0) same = false;
        else rest.splice(found, 1);
      });
      return same;
    }
    if (type === 'array' && a.length !== b.length) return false;
    const keysA = [];
    for (const key in a) keysA.push(key);
    let countB = 0;
    for (const key in b) countB++;
    if (keysA.length !== countB) return false;
    for (let i = 0; i < keysA.length; i++) {
      const key = keysA[i];
      if (!(key in b) || !deepEqual(a[key], b[key], seen)) return false;
    }
    return true;
  }

  const sizeOf = (o) =>
    typeof o === 'string' || isArray(o) ? o.length : o instanceof Map || o instanceof Set ? o.size : keysOf(O(o)).length;
  const contains = (h, n) =>
    typeof h === 'string' ? h.indexOf(n) >= 0
      : isArray(h) ? h.indexOf(n) >= 0
      : h instanceof Set || h instanceof Map ? h.has(n)
      : h !== null && typeof h === 'object' && keysOf(n).every((k) => h[k] === n[k]);
  const matches = (re, text) => {
    re.lastIndex = 0;
    return regexTest.call(re, text);
  };
  const messageOf = (error) => {
    try {
      return error && error.message !== undefined ? String(error.message) : String(error);
    } catch (e) {
      return '?';
    }
  };

  const assert = (value, message) => check(value, 'expected ' + show(value) + ' to be truthy', message);
  // BEGIN assert
  const api = {
  fail: (message) => fail(message || 'assert.fail()'),
  ok: (v, m) => check(v, `expected ${show(v)} to be truthy`, m),
  isOk: (v, m) => check(v, `expected ${show(v)} to be truthy`, m),
  notOk: (v, m) => check(!v, `expected ${show(v)} to be falsy`, m),
  isNotOk: (v, m) => check(!v, `expected ${show(v)} to be falsy`, m),
  equal: (a, b, m) => check(a == b, `expected ${show(a)} to equal ${show(b)}`, m), // eslint-disable-line eqeqeq
  notEqual: (a, b, m) => check(a != b, `expected ${show(a)} to not equal ${show(b)}`, m), // eslint-disable-line eqeqeq
  strictEqual: (a, b, m) => check(a === b, `expected ${show(a)} to equal ${show(b)}`, m),
  notStrictEqual: (a, b, m) => check(a !== b, `expected ${show(a)} to not equal ${show(b)}`, m),
  deepEqual: (a, b, m) => check(deepEqual(a, b), `expected ${show(a)} to deeply equal ${show(b)}`, m),
  deepStrictEqual: (a, b, m) => check(deepEqual(a, b), `expected ${show(a)} to deeply equal ${show(b)}`, m),
  notDeepEqual: (a, b, m) => check(!deepEqual(a, b), `expected ${show(a)} to not deeply equal ${show(b)}`, m),
  isTrue: (v, m) => check(v === true, `expected ${show(v)} to be true`, m),
  isNotTrue: (v, m) => check(v !== true, `expected ${show(v)} to not be true`, m),
  isFalse: (v, m) => check(v === false, `expected ${show(v)} to be false`, m),
  isNotFalse: (v, m) => check(v !== false, `expected ${show(v)} to not be false`, m),
  isNull: (v, m) => check(v === null, `expected ${show(v)} to equal null`, m),
  isNotNull: (v, m) => check(v !== null, `expected ${show(v)} to not equal null`, m),
  isUndefined: (v, m) => check(v === undefined, `expected ${show(v)} to equal undefined`, m),
  isDefined: (v, m) => check(v !== undefined, `expected ${show(v)} to not equal undefined`, m),
  exists: (v, m) => check(v !== null && v !== undefined, `expected ${show(v)} to exist`, m),
  notExists: (v, m) => check(v === null || v === undefined, `expected ${show(v)} to not exist`, m),
  isFunction: (v, m) => check(typeof v === 'function', `expected ${show(v)} to be a function`, m),
  isNotFunction: (v, m) => check(typeof v !== 'function', `expected ${show(v)} not to be a function`, m),
  isObject: (v, m) => check(kind(v) === 'object', `expected ${show(v)} to be an object`, m),
  isNotObject: (v, m) => check(kind(v) !== 'object', `expected ${show(v)} not to be an object`, m),
  isArray: (v, m) => check(isArray(v), `expected ${show(v)} to be an array`, m),
  isNotArray: (v, m) => check(!isArray(v), `expected ${show(v)} not to be an array`, m),
  isString: (v, m) => check(typeof v === 'string', `expected ${show(v)} to be a string`, m),
  isNotString: (v, m) => check(typeof v !== 'string', `expected ${show(v)} not to be a string`, m),
  isNumber: (v, m) => check(typeof v === 'number', `expected ${show(v)} to be a number`, m),
  isNotNumber: (v, m) => check(typeof v !== 'number', `expected ${show(v)} not to be a number`, m),
  isBoolean: (v, m) => check(typeof v === 'boolean', `expected ${show(v)} to be a boolean`, m),
  isNotBoolean: (v, m) => check(typeof v !== 'boolean', `expected ${show(v)} not to be a boolean`, m),
  isNaN: (v, m) => check(NumberIsNaN(v), `expected ${show(v)} to be NaN`, m),
  isNotNaN: (v, m) => check(!NumberIsNaN(v), `expected ${show(v)} not to be NaN`, m),
  isFinite: (v, m) => check(NumberIsFinite(v), `expected ${show(v)} to be finite`, m),
  typeOf: (v, t, m) => check(kind(v) === t, `expected ${show(v)} to be a ${t}`, m),
  notTypeOf: (v, t, m) => check(kind(v) !== t, `expected ${show(v)} not to be a ${t}`, m),
  instanceOf: (v, c, m) => check(v instanceof c, `expected ${show(v)} to be an instance of ${c && c.name}`, m),
  notInstanceOf: (v, c, m) => check(!(v instanceof c), `expected ${show(v)} to not be an instance of ${c && c.name}`, m),
  include: (h, n, m) => check(contains(h, n), `expected ${show(h)} to include ${show(n)}`, m),
  notInclude: (h, n, m) => check(!contains(h, n), `expected ${show(h)} to not include ${show(n)}`, m),
  includeMembers: (s, sub, m) =>
    check(sub.every((x) => s.indexOf(x) >= 0), `expected ${show(s)} to be a superset of ${show(sub)}`, m),
  sameMembers: (a, b, m) => {
    const rest = b.slice();
    const same = a.length === b.length && a.every((x) => {
      const i = rest.indexOf(x);
      if (i < 0) return false;
      rest.splice(i, 1);
      return true;
    });
    check(same, `expected ${show(a)} to have the same members as ${show(b)}`, m);
  },
  sameDeepMembers: (a, b, m) => {
    const rest = b.slice();
    const same = a.length === b.length && a.every((x) => {
      let found = -1;
      for (let i = 0; i < rest.length; i++) {
        if (deepEqual(x, rest[i])) {
          found = i;
          break;
        }
      }
      if (found < 0) return false;
      rest.splice(found, 1);
      return true;
    });
    check(same, `expected ${show(a)} to have the same members as ${show(b)}`, m);
  },
  property: (o, k, m) => check(o !== null && o !== undefined && k in O(o), `expected ${show(o)} to have property ${show(k)}`, m),
  notProperty: (o, k, m) => check(o === null || o === undefined || !(k in O(o)), `expected ${show(o)} to not have property ${show(k)}`, m),
  propertyVal: (o, k, v, m) => check(o[k] === v, `expected ${show(o)} to have property ${show(k)} of ${show(v)}`, m),
  containsAllKeys: (o, keys, m) =>
    check([].concat(keys).every((k) => k in O(o)), `expected ${show(o)} to contain all keys ${show(keys)}`, m),
  hasAllKeys: (o, keys, m) => {
    const wanted = [].concat(keys);
    const actual = keysOf(o);
    check(wanted.length === actual.length && wanted.every((k) => actual.indexOf(k) >= 0),
      `expected ${show(o)} to have all and only keys ${show(keys)}`, m);
  },
  lengthOf: (o, n, m) => check(o !== null && o !== undefined && o.length === n, `expected ${show(o)} to have a length of ${n}`, m),
  isEmpty: (o, m) => check(sizeOf(o) === 0, `expected ${show(o)} to be empty`, m),
  isNotEmpty: (o, m) => check(sizeOf(o) !== 0, `expected ${show(o)} not to be empty`, m),
  match: (s, re, m) => check(matches(re, s), `expected ${show(s)} to match ${re}`, m),
  notMatch: (s, re, m) => check(!matches(re, s), `expected ${show(s)} not to match ${re}`, m),
  isAbove: (a, b, m) => check(a > b, `expected ${show(a)} to be above ${show(b)}`, m),
  isAtLeast: (a, b, m) => check(a >= b, `expected ${show(a)} to be at least ${show(b)}`, m),
  isBelow: (a, b, m) => check(a < b, `expected ${show(a)} to be below ${show(b)}`, m),
  isAtMost: (a, b, m) => check(a <= b, `expected ${show(a)} to be at most ${show(b)}`, m),
  approximately: (a, b, d, m) => check(Math.abs(a - b) <= d, `expected ${show(a)} to be close to ${b} +/- ${d}`, m),
  closeTo: (a, b, d, m) => check(Math.abs(a - b) <= d, `expected ${show(a)} to be close to ${b} +/- ${d}`, m),
  // chai: throws(fn, [errorLike], [errMsgMatcher], [msg]); sem errorLike, o 2.º é já o errMsgMatcher.
  throws: (fn, a, b, c) => {
    const like = typeof a === 'function' || a instanceof Error ? a : null;
    const matcher = like === null ? a : b;
    const custom = like === null ? b : c;
    let thrown;
    let did = false;
    try {
      fn();
    } catch (e) {
      thrown = e;
      did = true;
    }
    check(did, 'expected function to throw an error', typeof custom === 'string' ? custom : undefined);
    if (typeof like === 'function') check(thrown instanceof like, `expected error to be an instance of ${like.name}, got ${show(thrown)}`, custom);
    else if (like instanceof Error) check(thrown === like, `expected function to throw ${show(like)}, got ${show(thrown)}`, custom);
    if (matcher instanceof RegExp) check(matches(matcher, messageOf(thrown)), `expected error message ${show(messageOf(thrown))} to match ${matcher}`, custom);
    else if (typeof matcher === 'string') check(messageOf(thrown).indexOf(matcher) >= 0, `expected error message ${show(messageOf(thrown))} to include ${show(matcher)}`, custom);
  },
  doesNotThrow: (fn, m) => {
    try {
      fn();
    } catch (e) {
      fail(typeof m === 'string' ? m : `expected function not to throw, but it threw ${show(e)}`);
    }
  },
  };
  // END assert
  for (const name of keysOf(api)) assert[name] = api[name];
  assert.AssertionError = AssertionError;

  // `__helpers` do freeCodeCamp (só os que os desafios Node-compatíveis usam).
  // BEGIN helpers
  const helpers = {
  removeWhiteSpace: (text) => String(text).replace(/\s/g, ''),
  // Tira os comentários `//` e `/* */`, respeitando strings e templates.
  removeJSComments: (source) => {
    const text = String(source);
    let out = '';
    let i = 0;
    while (i < text.length) {
      const c = text[i];
      const next = text[i + 1];
      if (c === '"' || c === "'" || c === '`') {
        let j = i + 1;
        while (j < text.length && text[j] !== c) j += text[j] === '\\' ? 2 : 1;
        out += text.slice(i, j + 1);
        i = j + 1;
      } else if (c === '/' && next === '/') {
        while (i < text.length && text[i] !== '\n') i++;
      } else if (c === '/' && next === '*') {
        const end = text.indexOf('*/', i + 2);
        i = end < 0 ? text.length : end + 2;
      } else {
        out += c;
        i++;
      }
    }
    return out;
  },
  };
  // END helpers

  const functions = [];
  return {
    // As funções dos testes criam-se já (o aluno ainda não correu): `assert`, `__helpers` e `code`
    // chegam por parâmetro, por isso o aluno não os pode substituir.
    prepare: (sources) => {
      for (let i = 0; i < sources.length; i++) {
        try {
          functions.push(new AsyncFunction('assert', '__helpers', 'code', sources[i]));
        } catch (error) {
          functions.push('FAIL: ' + messageOf(error));
        }
      }
    },
    run: async (index, code) => {
      const fn = functions[index];
      if (typeof fn !== 'function') return String(fn);
      try {
        await fn(assert, helpers, code);
        return 'PASS';
      } catch (error) {
        const name = error && error.name && error.name !== 'AssertionError' ? error.name + ': ' : '';
        return 'FAIL: ' + name + messageOf(error);
      }
    },
  };
})();
