package services

import (
	"bufio"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type LogFileInfo struct {
	ID         string    `json:"id"`
	Name       string    `json:"name"`
	Category   string    `json:"category"`
	RelPath    string    `json:"rel_path"`
	Size       int64     `json:"size"`
	UpdatedAt  time.Time `json:"updated_at"`
}

type LogTailResult struct {
	ID       string   `json:"id"`
	Lines    []string `json:"lines"`
	Total    int      `json:"total"`
	Tail     int      `json:"tail"`
	Truncated bool    `json:"truncated"`
}

type LogService struct {
	projectRoot string
}

func NewLogService(projectRoot string) *LogService {
	return &LogService{projectRoot: projectRoot}
}

func (s *LogService) encodeID(rel string) string {
	return strings.ReplaceAll(rel, "/", "::")
}

func (s *LogService) decodeID(id string) string {
	return strings.ReplaceAll(id, "::", "/")
}

func (s *LogService) allowedRoots() []string {
	return []string{
		filepath.Join(s.projectRoot, "data", "logs"),
		filepath.Join(s.projectRoot, "data", "dl_senpai"),
	}
}

func (s *LogService) resolveSafe(rel string) (string, error) {
	rel = filepath.Clean(strings.ReplaceAll(rel, "\\", "/"))
	if strings.HasPrefix(rel, "..") {
		return "", os.ErrPermission
	}
	abs := filepath.Join(s.projectRoot, rel)
	abs, err := filepath.Abs(abs)
	if err != nil {
		return "", err
	}
	if !strings.HasSuffix(strings.ToLower(abs), ".log") {
		return "", os.ErrPermission
	}
	for _, root := range s.allowedRoots() {
		rootAbs, _ := filepath.Abs(root)
		if strings.HasPrefix(abs, rootAbs+string(os.PathSeparator)) || abs == rootAbs {
			return abs, nil
		}
	}
	return "", os.ErrPermission
}

func (s *LogService) categoryFor(rel string) string {
	base := filepath.Base(rel)
	lower := strings.ToLower(base)
	switch {
	case strings.HasPrefix(lower, "bot-"):
		if strings.HasSuffix(lower, ".err.log") {
			return "bot-错误"
		}
		return "bot"
	case strings.HasPrefix(lower, "napcat-"):
		return "napcat"
	case strings.Contains(lower, "gptsovits"):
		return "语音 TTS"
	default:
		return "其它"
	}
}

func (s *LogService) ListFiles() ([]LogFileInfo, error) {
	var out []LogFileInfo
	for _, root := range s.allowedRoots() {
		_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
			if err != nil || info.IsDir() {
				return nil
			}
			if !strings.HasSuffix(strings.ToLower(info.Name()), ".log") {
				return nil
			}
			rel, err := filepath.Rel(s.projectRoot, path)
			if err != nil {
				return nil
			}
			rel = filepath.ToSlash(rel)
			out = append(out, LogFileInfo{
				ID:        s.encodeID(rel),
				Name:      info.Name(),
				Category:  s.categoryFor(rel),
				RelPath:   rel,
				Size:      info.Size(),
				UpdatedAt: info.ModTime(),
			})
			return nil
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].UpdatedAt.Equal(out[j].UpdatedAt) {
			return out[i].Name > out[j].Name
		}
		return out[i].UpdatedAt.After(out[j].UpdatedAt)
	})
	return out, nil
}

func (s *LogService) Tail(id string, n int) (*LogTailResult, error) {
	if n <= 0 {
		n = 200
	}
	if n > 2000 {
		n = 2000
	}
	rel := s.decodeID(id)
	abs, err := s.resolveSafe(rel)
	if err != nil {
		return nil, err
	}
	lines, truncated, err := readLastLines(abs, n)
	if err != nil {
		return nil, err
	}
	return &LogTailResult{
		ID:        id,
		Lines:     lines,
		Total:     len(lines),
		Tail:      n,
		Truncated: truncated,
	}, nil
}

func readLastLines(path string, n int) ([]string, bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, false, err
	}
	defer f.Close()

	stat, err := f.Stat()
	if err != nil {
		return nil, false, err
	}
	if stat.Size() == 0 {
		return []string{}, false, nil
	}

	// 小文件直接读全部
	if stat.Size() < 512*1024 {
		var all []string
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			all = append(all, sc.Text())
		}
		if err := sc.Err(); err != nil {
			return nil, false, err
		}
		if len(all) <= n {
			return all, false, nil
		}
		return all[len(all)-n:], true, nil
	}

	// 大文件从尾部块读
	const block = 8192
	var (
		buf      []byte
		lines    []string
		pos      = stat.Size()
		truncated bool
	)
	for pos > 0 && len(lines) < n {
		readSize := int64(block)
		if pos < readSize {
			readSize = pos
		}
		pos -= readSize
		chunk := make([]byte, readSize)
		if _, err := f.ReadAt(chunk, pos); err != nil {
			return nil, false, err
		}
		buf = append(chunk, buf...)
		parts := strings.Split(string(buf), "\n")
		if pos > 0 {
			buf = []byte(parts[0])
			parts = parts[1:]
		} else {
			buf = nil
		}
		for i := len(parts) - 1; i >= 0 && len(lines) < n; i-- {
			lines = append([]string{parts[i]}, lines...)
		}
	}
	if pos > 0 {
		truncated = true
	}
	return lines, truncated, nil
}
