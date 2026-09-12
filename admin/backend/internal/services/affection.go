package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type affectionTier struct {
	threshold int
	name      string
}

var affectionTiers = []affectionTier{
	{0, "路人"},
	{15, "面熟"},
	{40, "常来"},
	{80, "亲近"},
	{150, "信赖"},
	{300, "心头好"},
	{450, "热恋"},
	{700, "眷侣"},
}

func tierForValue(value int) string {
	name := affectionTiers[0].name
	for _, t := range affectionTiers {
		if value >= t.threshold {
			name = t.name
		}
	}
	return name
}

func nextTierForValue(value int) (name string, threshold int, ok bool) {
	for _, t := range affectionTiers {
		if value < t.threshold {
			return t.name, t.threshold, true
		}
	}
	return "", 0, false
}

type AffectionUser struct {
	UserID            string `json:"user_id"`
	DisplayName       string `json:"display_name"`
	Value             int    `json:"value"`
	TierTitle         string `json:"tier_title"`
	TierName          string `json:"tier_name"`
	NextTierName      string `json:"next_tier_name,omitempty"`
	NextTierThreshold int    `json:"next_tier_threshold,omitempty"`
	PointsToNext      int    `json:"points_to_next,omitempty"`
}

type AffectionGroup struct {
	GroupID   string    `json:"group_id"`
	ScopeType string    `json:"scope_type"` // group | private
	UserCount int       `json:"user_count"`
	UpdatedAt time.Time `json:"updated_at"`
}

func scopeTypeForID(id string) string {
	if strings.HasPrefix(id, "private_") {
		return "private"
	}
	return "group"
}

type affectionFile struct {
	Users     map[string]affectionUserRaw `json:"users"`
	UpdatedAt string                      `json:"updated_at"`
}

type affectionUserRaw struct {
	UserID      string `json:"user_id"`
	DisplayName string `json:"display_name"`
	Value       int    `json:"value"`
	TierTitle   string `json:"tier_title"`
}

type AffectionService struct {
	dir string
}

func NewAffectionService(dir string) *AffectionService {
	return &AffectionService{dir: dir}
}

func (s *AffectionService) enrichUser(uid string, raw affectionUserRaw) AffectionUser {
	value := raw.Value
	if value < 0 {
		value = 0
	}
	u := AffectionUser{
		UserID:      uid,
		DisplayName: raw.DisplayName,
		Value:       value,
		TierTitle:   raw.TierTitle,
		TierName:    tierForValue(value),
	}
	if nextName, nextThreshold, ok := nextTierForValue(value); ok {
		u.NextTierName = nextName
		u.NextTierThreshold = nextThreshold
		u.PointsToNext = nextThreshold - value
	}
	return u
}

func (s *AffectionService) ListGroups() ([]AffectionGroup, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return []AffectionGroup{}, nil
		}
		return nil, err
	}
	var out []AffectionGroup
	for _, e := range entries {
		if e.IsDir() || !strings.HasPrefix(e.Name(), "group_") || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		gid := strings.TrimSuffix(strings.TrimPrefix(e.Name(), "group_"), ".json")
		path := filepath.Join(s.dir, e.Name())
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var af affectionFile
		_ = json.Unmarshal(data, &af)
		out = append(out, AffectionGroup{
			GroupID:   gid,
			ScopeType: scopeTypeForID(gid),
			UserCount: len(af.Users),
			UpdatedAt: info.ModTime(),
		})
	}
	sortAffectionGroups(out)
	return out, nil
}

func sortAffectionGroups(groups []AffectionGroup) {
	sort.Slice(groups, func(i, j int) bool {
		a, b := groups[i], groups[j]
		if a.ScopeType != b.ScopeType {
			return a.ScopeType == "group"
		}
		return a.GroupID < b.GroupID
	})
}

func (s *AffectionService) GetUsers(groupID string) ([]AffectionUser, error) {
	path := filepath.Join(s.dir, "group_"+groupID+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []AffectionUser{}, nil
		}
		return nil, err
	}
	var af affectionFile
	if err := json.Unmarshal(data, &af); err != nil {
		return nil, err
	}
	users := make([]AffectionUser, 0, len(af.Users))
	for uid, u := range af.Users {
		users = append(users, s.enrichUser(uid, u))
	}
	return users, nil
}

type AffectionUserPatch struct {
	Value       *int    `json:"value"`
	DisplayName *string `json:"display_name"`
	TierTitle   *string `json:"tier_title"`
}

func (s *AffectionService) PatchUser(groupID, userID string, patch AffectionUserPatch) (*AffectionUser, error) {
	path := filepath.Join(s.dir, "group_"+groupID+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			data = []byte(`{"users":{}}`)
		} else {
			return nil, err
		}
	}
	var af affectionFile
	if err := json.Unmarshal(data, &af); err != nil {
		return nil, err
	}
	if af.Users == nil {
		af.Users = map[string]affectionUserRaw{}
	}
	u, ok := af.Users[userID]
	if !ok {
		u = affectionUserRaw{UserID: userID}
	}
	if patch.Value != nil {
		v := *patch.Value
		if v < 0 {
			v = 0
		}
		u.Value = v
	}
	if patch.DisplayName != nil {
		u.DisplayName = *patch.DisplayName
	}
	if patch.TierTitle != nil {
		u.TierTitle = *patch.TierTitle
	}
	af.Users[userID] = u
	af.UpdatedAt = time.Now().Format(time.RFC3339)
	out, err := json.MarshalIndent(af, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(s.dir, 0755); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, out, 0644); err != nil {
		return nil, err
	}
	enriched := s.enrichUser(userID, u)
	return &enriched, nil
}

func (s *AffectionService) TotalUsers() (int, error) {
	groups, err := s.ListGroups()
	if err != nil {
		return 0, err
	}
	total := 0
	for _, g := range groups {
		total += g.UserCount
	}
	return total, nil
}
