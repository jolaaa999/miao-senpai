package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type PendingTask struct {
	ID           string `json:"id"`
	Text         string `json:"text"`
	Reward       int    `json:"reward"`
	AssignedDate string `json:"assigned_date"`
}

type CheckinUser struct {
	UserID        string       `json:"user_id"`
	DisplayName   string       `json:"display_name"`
	Points        int          `json:"points"`
	Streak        int          `json:"streak"`
	MaxStreak     int          `json:"max_streak"`
	LastCheckin   string       `json:"last_checkin"`
	TotalCheckins int          `json:"total_checkins"`
	Title         string       `json:"title"`
	PendingTask   *PendingTask `json:"pending_task"`
}

type CheckinGroup struct {
	GroupID   string    `json:"group_id"`
	UserCount int       `json:"user_count"`
	UpdatedAt time.Time `json:"updated_at"`
}

type checkinFile struct {
	Users     map[string]CheckinUser `json:"users"`
	UpdatedAt string                 `json:"updated_at"`
}

type CheckinService struct {
	dir string
}

func NewCheckinService(dir string) *CheckinService {
	return &CheckinService{dir: dir}
}

func (s *CheckinService) ListGroups() ([]CheckinGroup, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return []CheckinGroup{}, nil
		}
		return nil, err
	}
	var out []CheckinGroup
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
		var cf checkinFile
		_ = json.Unmarshal(data, &cf)
		out = append(out, CheckinGroup{
			GroupID:   gid,
			UserCount: len(cf.Users),
			UpdatedAt: info.ModTime(),
		})
	}
	return out, nil
}

func (s *CheckinService) GetUsers(groupID string) ([]CheckinUser, error) {
	path := filepath.Join(s.dir, "group_"+groupID+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []CheckinUser{}, nil
		}
		return nil, err
	}
	var cf checkinFile
	if err := json.Unmarshal(data, &cf); err != nil {
		return nil, err
	}
	users := make([]CheckinUser, 0, len(cf.Users))
	for uid, u := range cf.Users {
		u.UserID = uid
		users = append(users, u)
	}
	return users, nil
}

type CheckinUserPatch struct {
	Points      *int    `json:"points"`
	Streak      *int    `json:"streak"`
	Title       *string `json:"title"`
	DisplayName *string `json:"display_name"`
}

func (s *CheckinService) PatchUser(groupID, userID string, patch CheckinUserPatch) (*CheckinUser, error) {
	path := filepath.Join(s.dir, "group_"+groupID+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cf checkinFile
	if err := json.Unmarshal(data, &cf); err != nil {
		return nil, err
	}
	if cf.Users == nil {
		cf.Users = map[string]CheckinUser{}
	}
	u, ok := cf.Users[userID]
	if !ok {
		u = CheckinUser{UserID: userID}
	}
	if patch.Points != nil {
		u.Points = *patch.Points
	}
	if patch.Streak != nil {
		u.Streak = *patch.Streak
	}
	if patch.Title != nil {
		u.Title = *patch.Title
	}
	if patch.DisplayName != nil {
		u.DisplayName = *patch.DisplayName
	}
	cf.Users[userID] = u
	cf.UpdatedAt = time.Now().Format(time.RFC3339)
	out, err := json.MarshalIndent(cf, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, out, 0644); err != nil {
		return nil, err
	}
	u.UserID = userID
	return &u, nil
}
