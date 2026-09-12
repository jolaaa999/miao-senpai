package services

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// 与 bot 侧 src/plugins/dl_senpai/group_features.py 的 FEATURES 保持一致。
var GroupFeatureCatalog = []struct {
	Key   string `json:"key"`
	Label string `json:"label"`
}{
	{"checkin", "签到"},
	{"affection", "好感度"},
	{"shop", "积分商店"},
	{"draw", "生图"},
	{"card", "卡片图回复"},
	{"mute", "生气禁言"},
	{"browser", "联网浏览"},
	{"search", "网络搜索"},
	{"sticker", "表情包"},
	{"voice", "语音回复"},
	{"welcome", "入群欢迎"},
	{"verify", "进群认证"},
	{"group_style", "群风格"},
	{"memory_recall", "记忆回想"},
	{"person_memory", "个人记忆"},
	{"inner_state", "内心状态"},
	{"vision", "识图"},
	{"trends", "热点趋势"},
	{"friend_add", "自动加好友"},
	{"like", "每日点赞"},
	{"qzone", "空间动态赞评"},
}

var groupFeatureKeySet = func() map[string]bool {
	set := make(map[string]bool, len(GroupFeatureCatalog))
	for _, f := range GroupFeatureCatalog {
		set[f.Key] = true
	}
	return set
}()

// GroupFeatureEntry：features 里 nil 缺省 = 跟随全局；指针值 = 覆盖。
type GroupFeatureEntry struct {
	GroupID   string           `json:"group_id"`
	Name      string           `json:"name"`
	Features  map[string]*bool `json:"features"`
	UpdatedAt string           `json:"updated_at"`
}

type groupFeatureRaw struct {
	Name      string           `json:"name"`
	UpdatedAt string           `json:"updated_at"`
	Features  map[string]*bool `json:"features"`
}

type groupFeaturesFile struct {
	Version int                       `json:"version"`
	Groups  map[string]*groupFeatureRaw `json:"groups"`
}

type GroupFeaturePatch struct {
	Feature string `json:"feature" binding:"required"`
	Value   *bool  `json:"value"` // nil = 恢复跟随全局
}

type GroupFeaturesService struct {
	path string
	mu   sync.Mutex
}

func NewGroupFeaturesService(path string) *GroupFeaturesService {
	return &GroupFeaturesService{path: path}
}

func (s *GroupFeaturesService) load() (*groupFeaturesFile, error) {
	data, err := os.ReadFile(s.path)
	if err != nil {
		if os.IsNotExist(err) {
			return &groupFeaturesFile{Version: 1, Groups: map[string]*groupFeatureRaw{}}, nil
		}
		return nil, err
	}
	var file groupFeaturesFile
	if err := json.Unmarshal(data, &file); err != nil {
		return nil, err
	}
	if file.Groups == nil {
		file.Groups = map[string]*groupFeatureRaw{}
	}
	file.Version = 1
	return &file, nil
}

func (s *GroupFeaturesService) save(file *groupFeaturesFile) error {
	out, err := json.MarshalIndent(file, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0755); err != nil {
		return err
	}
	// 与 bot 侧（.json.tmp）错开，避免双端并发写同一临时文件
	tmp := s.path + ".go.tmp"
	if err := os.WriteFile(tmp, out, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *GroupFeaturesService) List() ([]GroupFeatureEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	file, err := s.load()
	if err != nil {
		return nil, err
	}
	out := make([]GroupFeatureEntry, 0, len(file.Groups))
	for gid, raw := range file.Groups {
		if raw == nil {
			continue
		}
		out = append(out, GroupFeatureEntry{
			GroupID:   gid,
			Name:      raw.Name,
			Features:  raw.Features,
			UpdatedAt: raw.UpdatedAt,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i].GroupID, out[j].GroupID
		if len(a) != len(b) {
			return len(a) < len(b)
		}
		return a < b
	})
	return out, nil
}

func (s *GroupFeaturesService) Patch(groupID, feature string, value *bool) (*GroupFeatureEntry, error) {
	groupID = strings.TrimSpace(groupID)
	if groupID == "" {
		return nil, fmt.Errorf("group_id 不能为空")
	}
	if !groupFeatureKeySet[feature] {
		return nil, fmt.Errorf("未知功能: %s", feature)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	file, err := s.load()
	if err != nil {
		return nil, err
	}
	raw := file.Groups[groupID]
	if raw == nil {
		raw = &groupFeatureRaw{Features: map[string]*bool{}}
		file.Groups[groupID] = raw
	}
	if raw.Features == nil {
		raw.Features = map[string]*bool{}
	}
	if value == nil {
		delete(raw.Features, feature)
	} else {
		raw.Features[feature] = value
	}
	raw.UpdatedAt = time.Now().Format(time.RFC3339)
	if err := s.save(file); err != nil {
		return nil, err
	}
	return &GroupFeatureEntry{
		GroupID:   groupID,
		Name:      raw.Name,
		Features:  raw.Features,
		UpdatedAt: raw.UpdatedAt,
	}, nil
}
