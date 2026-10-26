package services

// Agent 调用链（trace）读取服务。
//
// 数据来源刻意**不是数据库**：bot 侧每条请求结束会往 bot-YYYYMMDD.log 写一行
// `agent.request_summary ...`（见 src/plugins/dl_senpai/agent/entry.py）。
// 那行已经是"一条消息的完整结论"，再落一张表就是第二份真相，
// 还要处理保留期、迁移、和日志不一致的问题。所以这里只读日志、只解析。
//
// 代价：日志被轮转/清理后历史就没了。这是可接受的——排查的是"刚刚那条为什么这样回"，
// 不是长期指标（长期指标走 Validation JSONL + 离线报告）。

import (
	"bufio"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// AgentTrace 一行 agent.request_summary 的结构化视图。
//
// 只挑现场最常看的字段；完整原文保留在 Raw 里，方便"解析漏了字段"时不至于丢信息。
type AgentTrace struct {
	Time       string  `json:"time"`
	SessionID  string  `json:"session_id"`
	Trace      string  `json:"trace"`
	Mode       string  `json:"mode"`
	ReplyBy    string  `json:"reply_by"`
	Stop       string  `json:"stop"`
	Trigger    string  `json:"trigger"`
	TotalMs    float64 `json:"total_ms"`
	QueueMs    float64 `json:"queue_ms"`
	ModelMs    float64 `json:"model_ms"`
	ToolMs     float64 `json:"tool_ms"`
	SendMs     float64 `json:"send_ms"`
	ModelCalls int     `json:"model_calls"`
	Usage      int     `json:"usage"`
	ToolCalls  int     `json:"tool_calls"`
	Tools      string  `json:"tools"`
	Capability string  `json:"capabilities"`
	Error      string  `json:"error"`
	File       string  `json:"file"`
	Raw        string  `json:"raw"`
}

// TraceQuery 过滤条件。空字段表示不过滤。
type TraceQuery struct {
	Session string
	Keyword string // 在整个 key=value 串里做子串匹配（工具名、错误、trace 都覆盖）
	ReplyBy string
	Mode    string
	Limit   int
}

type TraceResult struct {
	Items   []AgentTrace `json:"items"`
	Scanned int          `json:"scanned"`
	Files   []string     `json:"files"`
	Notice  string       `json:"notice"`
}

type TraceService struct {
	projectRoot string
}

func NewTraceService(projectRoot string) *TraceService {
	return &TraceService{projectRoot: projectRoot}
}

// logFiles 返回最近的 bot 日志（新→旧）。
//
// 只取 bot-*.log：NapCat/GPT-SoVITS 的日志里不会有 agent.request_summary，
// 扫它们纯属浪费 IO。最多看 5 个文件，够覆盖最近几天。
func (s *TraceService) logFiles(max int) []string {
	root := filepath.Join(s.projectRoot, "data", "logs")
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	type fileInfo struct {
		path string
		mod  time.Time
		name string
	}
	var cands []fileInfo
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		lower := strings.ToLower(name)
		if !strings.HasPrefix(lower, "bot-") || !strings.HasSuffix(lower, ".log") {
			continue
		}
		if strings.HasSuffix(lower, ".err.log") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		cands = append(cands, fileInfo{path: filepath.Join(root, name), mod: info.ModTime(), name: name})
	}
	sort.Slice(cands, func(i, j int) bool { return cands[i].mod.After(cands[j].mod) })
	if max > 0 && len(cands) > max {
		cands = cands[:max]
	}
	out := make([]string, 0, len(cands))
	for _, c := range cands {
		out = append(out, c.path)
	}
	return out
}

// Query 扫描日志并返回最近的 trace 行（新→旧）。
func (s *TraceService) Query(q TraceQuery) *TraceResult {
	if q.Limit <= 0 {
		q.Limit = 200
	}
	if q.Limit > 1000 {
		q.Limit = 1000
	}
	files := s.logFiles(5)
	res := &TraceResult{Items: []AgentTrace{}}
	for _, path := range files {
		res.Files = append(res.Files, filepath.Base(path))
	}
	if len(files) == 0 {
		res.Notice = "data/logs 下没有 bot-*.log；确认 bot 已启动过并按日志配置落盘。"
		return res
	}

	var rows []AgentTrace
	for _, path := range files {
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		sc := bufio.NewScanner(f)
		// 单行可能很长（tools= 与 capabilities= 都在里面），默认 64KB 会截断。
		sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for sc.Scan() {
			line := sc.Text()
			res.Scanned++
			idx := strings.Index(line, "agent.request_summary")
			if idx < 0 {
				continue
			}
			rows = append(rows, buildTraceRow(line, line[idx+len("agent.request_summary"):], filepath.Base(path)))
		}
		_ = f.Close()
	}

	// 文件本身是"新→旧"、单文件内是"旧→新"，整体反转即得全局新→旧。
	for i, j := 0, len(rows)-1; i < j; i, j = i+1, j-1 {
		rows[i], rows[j] = rows[j], rows[i]
	}

	for _, row := range rows {
		if !matchesTrace(row, q) {
			continue
		}
		res.Items = append(res.Items, row)
		if len(res.Items) >= q.Limit {
			break
		}
	}
	if len(rows) == 0 {
		res.Notice = "日志里没有 agent.request_summary 行：确认 DL_SENPAI_AGENT_TRACE_SUMMARY 未被关闭，"
		res.Notice += "且 bot 已重启到带该功能的版本。"
	}
	return res
}

func matchesTrace(row AgentTrace, q TraceQuery) bool {
	if q.Session != "" && row.SessionID != q.Session {
		return false
	}
	if q.ReplyBy != "" && row.ReplyBy != q.ReplyBy {
		return false
	}
	if q.Mode != "" && row.Mode != q.Mode {
		return false
	}
	if q.Keyword != "" && !strings.Contains(strings.ToLower(row.Raw), strings.ToLower(q.Keyword)) {
		return false
	}
	return true
}

func buildTraceRow(line, rest, file string) AgentTrace {
	fields := parseTraceFields(rest)
	row := AgentTrace{
		Time:       traceTimestamp(line),
		SessionID:  fields["session_id"],
		Trace:      fields["trace"],
		Mode:       fields["mode"],
		ReplyBy:    fields["reply_by"],
		Stop:       fields["stop"],
		Trigger:    fields["trigger"],
		TotalMs:    parseFloat(fields["total_ms"]),
		QueueMs:    parseFloat(fields["queue_ms"]),
		ModelMs:    parseFloat(fields["model_ms"]),
		ToolMs:     parseFloat(fields["tool_ms"]),
		SendMs:     parseFloat(fields["send_ms"]),
		ModelCalls: parseInt(fields["model_calls"]),
		Usage:      parseInt(fields["usage"]),
		ToolCalls:  parseInt(fields["tool_calls"]),
		Tools:      fields["tools"],
		Capability: fields["capabilities"],
		Error:      fields["error"],
		File:       file,
		Raw:        strings.TrimSpace(line),
	}
	return row
}

// traceTimestamp 取 loguru 行首的 "MM-DD HH:MM:SS"。
//
// 不解析成 time.Time 再格式化：日志里的时间是**本地时间且没有年份**，
// 硬转成 RFC3339 反而会让人误以为是精确时刻。原样返回最诚实。
func traceTimestamp(line string) string {
	end := strings.IndexByte(line, '[')
	if end < 0 {
		return ""
	}
	return strings.TrimSpace(line[:end])
}

func parseFloat(s string) float64 {
	v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return 0
	}
	return v
}

func parseInt(s string) int {
	v, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil {
		return 0
	}
	return v
}

// parseTraceFields 解析 `k=v k2="v 2"` 形式的字段串。
//
// 为什么要手写而不是按空格切：values 里有带空格的引号值（tools="a:ok:1ms,b:fail:2ms"
// 本身不带空格，但 capabilities 或错误信息会带），按空格切会把一个值碎成多段。
func parseTraceFields(s string) map[string]string {
	out := make(map[string]string, 24)
	i := 0
	n := len(s)
	for i < n {
		for i < n && s[i] == ' ' {
			i++
		}
		if i >= n {
			break
		}
		start := i
		for i < n && s[i] != '=' && s[i] != ' ' {
			i++
		}
		if i >= n || s[i] != '=' {
			continue
		}
		key := s[start:i]
		i++ // 跳过 =
		var val string
		if i < n && s[i] == '"' {
			i++
			var b strings.Builder
			for i < n {
				if s[i] == '\\' && i+1 < n {
					b.WriteByte(s[i+1])
					i += 2
					continue
				}
				if s[i] == '"' {
					i++
					break
				}
				b.WriteByte(s[i])
				i++
			}
			val = b.String()
		} else {
			start = i
			for i < n && s[i] != ' ' {
				i++
			}
			val = s[start:i]
		}
		if key != "" {
			out[key] = val
		}
	}
	return out
}
