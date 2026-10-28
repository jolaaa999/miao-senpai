package services

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// DiaryEntry：学姐「自己的生活」里的一条日记。
// 数据源是 bot 侧 own_life / diary 写下的 index.jsonl。
type DiaryEntry struct {
	ID        string   `json:"id"`
	Day       string   `json:"day"`
	Title     string   `json:"title"`
	Text      string   `json:"text"`
	Mood      string   `json:"mood"`
	Angle     string   `json:"angle"`
	Tier      string   `json:"tier"`
	Tags      []string `json:"tags"`
	LifeDays  int      `json:"life_days"`
	CreatedAt string   `json:"created_at"`
}

// DiaryDay：按天汇总，前端拿来渲染日历式筛选。
type DiaryDay struct {
	Day   string `json:"day"`
	Count int    `json:"count"`
}

type DiarySnapshot struct {
	Items []DiaryEntry `json:"items"`
	Days  []DiaryDay   `json:"days"`
	Total int          `json:"total"`
	Path  string       `json:"path"`
}

// diaryRaw 对应 index.jsonl 的原始行：时间是 unix 秒，deleted_at 非 0 表示这条被删过。
type diaryRaw struct {
	ID        string   `json:"id"`
	Day       string   `json:"day"`
	Title     string   `json:"title"`
	Text      string   `json:"text"`
	Mood      string   `json:"mood"`
	Angle     string   `json:"angle"`
	Tier      string   `json:"tier"`
	Tags      []string `json:"tags"`
	LifeDays  int      `json:"life_days"`
	T         float64  `json:"t"`
	DeletedAt float64  `json:"deleted_at"`
}

type DiaryService struct {
	indexPath string
}

func NewDiaryService(dir string) *DiaryService {
	return &DiaryService{indexPath: filepath.Join(dir, "index.jsonl")}
}

// List 读取整份日记索引，按时间倒序返回；limit<=0 表示不截断。
func (s *DiaryService) List(limit int) (DiarySnapshot, error) {
	snap := DiarySnapshot{Items: []DiaryEntry{}, Days: []DiaryDay{}, Path: s.indexPath}
	f, err := os.Open(s.indexPath)
	if err != nil {
		if os.IsNotExist(err) {
			return snap, nil
		}
		return snap, err
	}
	defer f.Close()

	counts := map[string]int{}
	scanner := bufio.NewScanner(f)
	// 一行是一条完整日记，默认 64KB 上限不够时放宽，避免长文本被截断
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var raw diaryRaw
		if err := json.Unmarshal([]byte(line), &raw); err != nil {
			// 坏行跳过：索引是增量追加写的，一行坏了不该让整页挂掉
			continue
		}
		if raw.DeletedAt != 0 {
			continue
		}
		entry := DiaryEntry{
			ID:       raw.ID,
			Day:      raw.Day,
			Title:    raw.Title,
			Text:     raw.Text,
			Mood:     raw.Mood,
			Angle:    raw.Angle,
			Tier:     raw.Tier,
			Tags:     raw.Tags,
			LifeDays: raw.LifeDays,
		}
		if entry.Tags == nil {
			entry.Tags = []string{}
		}
		if raw.T > 0 {
			entry.CreatedAt = time.Unix(int64(raw.T), 0).Format("2006-01-02 15:04:05")
		}
		snap.Items = append(snap.Items, entry)
		counts[raw.Day]++
	}
	if err := scanner.Err(); err != nil {
		return snap, err
	}

	// 时间格式固定为 "2006-01-02 15:04:05"，字符串倒序即时间倒序
	sort.SliceStable(snap.Items, func(i, j int) bool {
		return snap.Items[i].CreatedAt > snap.Items[j].CreatedAt
	})
	snap.Total = len(snap.Items)
	if limit > 0 && len(snap.Items) > limit {
		snap.Items = snap.Items[:limit]
	}

	for day, count := range counts {
		snap.Days = append(snap.Days, DiaryDay{Day: day, Count: count})
	}
	sort.Slice(snap.Days, func(i, j int) bool { return snap.Days[i].Day > snap.Days[j].Day })
	return snap, nil
}
