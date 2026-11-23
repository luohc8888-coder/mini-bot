/**
 * 轻量零依赖多语言语法高亮分词器（支持 Python / TS / TSX / JS / JSX / Java / JSON / CSS）。
 *
 * 输出纯数据结构，由 React 直接渲染为 `<span>`，避免 `dangerouslySetInnerHTML`，
 * 并通过 CSS 变量原生适配夜间/日光双主题。
 */

export type SyntaxTokenKind =
  | "plain"
  | "keyword"
  | "string"
  | "comment"
  | "number"
  | "decorator"
  | "type"
  | "function";

export interface SyntaxToken {
  kind: SyntaxTokenKind;
  text: string;
}

const PY_KEYWORDS = new Set([
  "and", "as", "assert", "async", "await", "break", "class", "continue",
  "def", "del", "elif", "else", "except", "False", "finally", "for",
  "from", "global", "if", "import", "in", "is", "lambda", "None",
  "nonlocal", "not", "or", "pass", "raise", "return", "True", "try",
  "while", "with", "yield", "match", "case", "self", "cls",
]);

const TS_KEYWORDS = new Set([
  "as", "async", "await", "break", "case", "catch", "class", "const",
  "continue", "debugger", "default", "delete", "do", "else", "enum",
  "export", "extends", "false", "finally", "for", "from", "function",
  "if", "implements", "import", "in", "instanceof", "interface", "let",
  "new", "null", "of", "private", "protected", "public", "readonly",
  "return", "static", "super", "switch", "this", "throw", "true", "try",
  "type", "typeof", "undefined", "var", "void", "while", "yield",
]);

const JAVA_KEYWORDS = new Set([
  "abstract", "assert", "boolean", "break", "byte", "case", "catch",
  "char", "class", "const", "continue", "default", "do", "double", "else",
  "enum", "extends", "false", "final", "finally", "float", "for", "if",
  "implements", "import", "instanceof", "int", "interface", "long",
  "native", "new", "null", "package", "private", "protected", "public",
  "record", "return", "short", "static", "super", "switch", "synchronized",
  "this", "throw", "throws", "transient", "true", "try", "var", "void",
  "volatile", "while",
]);

const BUILTIN_TYPES = new Set([
  "str", "int", "float", "bool", "bytes", "dict", "list", "set", "tuple",
  "object", "Any", "Optional", "Union", "Callable", "Sequence", "Mapping",
  "string", "number", "boolean", "unknown", "never", "Record", "Promise",
  "Array", "Map", "Set", "Partial", "Pick", "Omit", "String", "Integer",
  "Long", "Double", "Boolean", "List", "Object", "Void",
]);

export function inferLanguage(language?: string | null, filePath?: string | null): string {
  const raw = (language ?? "").trim().toLowerCase();
  if (raw) {
    if (raw === "py") return "python";
    if (raw === "ts" || raw === "tsx") return "typescript";
    if (raw === "js" || raw === "jsx") return "javascript";
    return raw;
  }
  const lower = (filePath ?? "").toLowerCase();
  if (lower.endsWith(".py")) return "python";
  if (lower.endsWith(".ts") || lower.endsWith(".tsx")) return "typescript";
  if (lower.endsWith(".js") || lower.endsWith(".jsx") || lower.endsWith(".mjs")) return "javascript";
  if (lower.endsWith(".java")) return "java";
  if (lower.endsWith(".json")) return "json";
  if (lower.endsWith(".css") || lower.endsWith(".scss")) return "css";
  return "plain";
}

function keywordsFor(lang: string): Set<string> {
  if (lang === "python") return PY_KEYWORDS;
  if (lang === "typescript" || lang === "javascript") return TS_KEYWORDS;
  if (lang === "java") return JAVA_KEYWORDS;
  return new Set();
}

function isIdentStart(ch: string): boolean {
  const code = ch.charCodeAt(0);
  return (
    (code >= 65 && code <= 90) ||
    (code >= 97 && code <= 122) ||
    code === 95 ||
    code === 36
  );
}

function isIdentPart(ch: string): boolean {
  const code = ch.charCodeAt(0);
  return isIdentStart(ch) || (code >= 48 && code <= 57);
}

function isDigit(ch: string): boolean {
  const code = ch.charCodeAt(0);
  return code >= 48 && code <= 57;
}

function pushToken(tokens: SyntaxToken[], kind: SyntaxTokenKind, text: string): void {
  if (!text) return;
  const last = tokens[tokens.length - 1];
  if (last && last.kind === kind) {
    last.text += text;
  } else {
    tokens.push({ kind, text });
  }
}

/**
 * 将单行源码分词为语法高亮 Token 数组。
 */
export function tokenizeCodeLine(
  line: string,
  language?: string | null,
  filePath?: string | null,
): SyntaxToken[] {
  if (!line) {
    return [{ kind: "plain", text: " " }];
  }
  const lang = inferLanguage(language, filePath);
  const keywords = keywordsFor(lang);
  const isPython = lang === "python";
  const isCStyle =
    lang === "typescript" ||
    lang === "javascript" ||
    lang === "java" ||
    lang === "css";

  const tokens: SyntaxToken[] = [];
  const n = line.length;
  let i = 0;

  while (i < n) {
    const ch = line[i];

    // 1. 单行注释
    if (isPython && ch === "#") {
      pushToken(tokens, "comment", line.slice(i));
      break;
    }
    if (isCStyle && ch === "/" && i + 1 < n && line[i + 1] === "/") {
      pushToken(tokens, "comment", line.slice(i));
      break;
    }
    // 行内块注释 /* ... */ 或文档注释续行 *
    if (isCStyle && ch === "/" && i + 1 < n && line[i + 1] === "*") {
      const end = line.indexOf("*/", i + 2);
      if (end === -1) {
        pushToken(tokens, "comment", line.slice(i));
        break;
      }
      pushToken(tokens, "comment", line.slice(i, end + 2));
      i = end + 2;
      continue;
    }

    // 2. Python 装饰器 / Java 注解 (@...)
    if (
      (isPython || lang === "java" || lang === "typescript") &&
      ch === "@" &&
      i + 1 < n &&
      isIdentStart(line[i + 1])
    ) {
      let j = i + 2;
      while (j < n && (isIdentPart(line[j]) || line[j] === ".")) {
        j += 1;
      }
      pushToken(tokens, "decorator", line.slice(i, j));
      i = j;
      continue;
    }

    // 3. 字符串（含 Python 前缀 f/r/b/u 与三引号）
    const pyPrefix =
      isPython &&
      "fFrRbBuU".includes(ch) &&
      i + 1 < n &&
      (line[i + 1] === '"' || line[i + 1] === "'");
    if (pyPrefix || ch === '"' || ch === "'" || (isCStyle && ch === "`")) {
      const start = i;
      if (pyPrefix) {
        i += 1;
      }
      const quote = line[i];
      const triple =
        isPython &&
        i + 2 < n &&
        line[i + 1] === quote &&
        line[i + 2] === quote;
      if (triple) {
        const closeIdx = line.indexOf(quote.repeat(3), i + 3);
        if (closeIdx === -1) {
          pushToken(tokens, "string", line.slice(start));
          break;
        }
        pushToken(tokens, "string", line.slice(start, closeIdx + 3));
        i = closeIdx + 3;
        continue;
      }
      i += 1;
      while (i < n) {
        if (line[i] === "\\" && i + 1 < n) {
          i += 2;
          continue;
        }
        if (line[i] === quote) {
          i += 1;
          break;
        }
        i += 1;
      }
      pushToken(tokens, "string", line.slice(start, i));
      continue;
    }

    // 4. 数字字面量
    if (
      isDigit(ch) &&
      (i === 0 || !isIdentPart(line[i - 1]))
    ) {
      let j = i + 1;
      while (j < n && /[0-9a-fA-FxXoObB._eE]/.test(line[j])) {
        j += 1;
      }
      pushToken(tokens, "number", line.slice(i, j));
      i = j;
      continue;
    }

    // 5. 标识符（关键字 / 类型 / 函数调用 / 普通符号）
    if (isIdentStart(ch)) {
      let j = i + 1;
      while (j < n && isIdentPart(line[j])) {
        j += 1;
      }
      const word = line.slice(i, j);
      if (keywords.has(word)) {
        pushToken(tokens, "keyword", word);
      } else if (
        BUILTIN_TYPES.has(word) ||
        (word.length > 1 && word[0] >= "A" && word[0] <= "Z" && /[a-z]/.test(word))
      ) {
        pushToken(tokens, "type", word);
      } else if (j < n && line[j] === "(") {
        pushToken(tokens, "function", word);
      } else {
        pushToken(tokens, "plain", word);
      }
      i = j;
      continue;
    }

    // 6. 其他标点/空白
    pushToken(tokens, "plain", ch);
    i += 1;
  }

  return tokens;
}
