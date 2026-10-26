package services

// trace 解析的单元测试。
//
// 为什么必须有：日志格式是**跨语言契约**（Python 写、Go 读），
// 任何一侧改一个字符（例如把 tools="a,b" 改成 tools=a,b）都会让控制台静默变空，
// 而那种"页面没数据"的问题在线上极难归因。用一条真实格式的行钉住它。

import (
	"testing"
)

const sampleLine = `10-26 10:27:52 [INFO] dl_senpai.observability.logging | agent.request_summary session_id=group:1 stage=summary trace=abc mode=runtime reply_by=runtime stop=finished trigger=mention interrupt=False total_ms=1234.5 queue_ms=0 model_ms=800.25 tool_ms=123.4 send_ms=90 model_calls=1 usage=1234 tokens_unknown=None model_errors=None retries=None tools="search:success:123ms,+2" tool_calls=3 history=0 recalled=0 send_ok=1 send_fail=0 bubbles=2 capabilities="visible=9;registered=26;sources=builtin:1" error=None`

func TestParseTraceFields(t *testing.T) {
	fields := parseTraceFields(sampleLine[len("10-26 10:27:52 [INFO] dl_senpai.observability.logging | agent.request_summary"):])
	cases := map[string]string{
		"session_id": "group:1",
		"trace":      "abc",
		"reply_by":   "runtime",
		"stop":       "finished",
		"tools":      "search:success:123ms,+2", // 引号内的逗号与加号不能被切开
		"capabilities": "visible=9;registered=26;sources=builtin:1", // 值里带 = 也要完整
	}
	for key, want := range cases {
		if got := fields[key]; got != want {
			t.Fatalf("%s = %q, want %q", key, got, want)
		}
	}
}

func TestBuildTraceRow(t *testing.T) {
	idx := 0
	for i := 0; i+len("agent.request_summary") <= len(sampleLine); i++ {
		if sampleLine[i:i+len("agent.request_summary")] == "agent.request_summary" {
			idx = i + len("agent.request_summary")
			break
		}
	}
	row := buildTraceRow(sampleLine, sampleLine[idx:], "bot-20261026.log")
	if row.Time != "10-26 10:27:52" {
		t.Fatalf("time = %q", row.Time)
	}
	if row.SessionID != "group:1" || row.Mode != "runtime" || row.ReplyBy != "runtime" {
		t.Fatalf("基本字段解析错误: %+v", row)
	}
	if row.TotalMs != 1234.5 {
		t.Fatalf("total_ms = %v", row.TotalMs)
	}
	if row.ModelCalls != 1 || row.ToolCalls != 3 || row.Usage != 1234 {
		t.Fatalf("计数字段解析错误: %+v", row)
	}
	if row.File != "bot-20261026.log" {
		t.Fatalf("file = %q", row.File)
	}
}

func TestMatchesTrace(t *testing.T) {
	row := AgentTrace{SessionID: "group:1", ReplyBy: "runtime", Mode: "runtime", Raw: sampleLine}
	if !matchesTrace(row, TraceQuery{Session: "group:1"}) {
		t.Fatal("同会话应命中")
	}
	if matchesTrace(row, TraceQuery{Session: "group:2"}) {
		t.Fatal("不同会话不应命中")
	}
	if !matchesTrace(row, TraceQuery{Keyword: "SEARCH"}) {
		t.Fatal("关键词应大小写不敏感")
	}
	if matchesTrace(row, TraceQuery{ReplyBy: "safe_fallback"}) {
		t.Fatal("reply_by 过滤失效")
	}
}
