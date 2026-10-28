package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// OutfitCatalogItem：服装店货架总目录里的一件衣服。
type OutfitCatalogItem struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Price    int    `json:"price"`
	OutfitEN string `json:"outfit_en"`
	Thumb    string `json:"thumb"` // 本地缩略图文件名，没有图时为空
}

// OutfitSlot：某群今天刷出来的货架格子。
type OutfitSlot struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type ShopGroup struct {
	GroupID            string       `json:"group_id"`
	Name               string       `json:"name"`
	Slots              []OutfitSlot `json:"slots"`
	CurrentOutfitID    string       `json:"current_outfit_id"`
	CurrentOutfitName  string       `json:"current_outfit_name"`
	SlotsRefreshDay    string       `json:"slots_refresh_day"`
	UpdatedAt          string       `json:"updated_at"`
	WarehouseUserCount int          `json:"warehouse_user_count"`
	OwnedCount         int          `json:"owned_count"`
}

type OutfitShopSnapshot struct {
	Catalog     []OutfitCatalogItem `json:"catalog"`
	Groups      []ShopGroup         `json:"groups"`
	ThumbDir    string              `json:"thumb_dir"`
	CatalogPath string              `json:"catalog_path"`
}

type shopCatalogFile struct {
	Items []struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Price    int    `json:"price"`
		OutfitEN string `json:"outfit_en"`
	} `json:"items"`
}

// shopWarehouseItem：玩家衣柜里的一条购买记录。
type shopWarehouseItem struct {
	ItemID      string `json:"item_id"`
	BuyerID     string `json:"buyer_id"`
	BuyerName   string `json:"buyer_name"`
	PurchasedAt string `json:"purchased_at"`
}

type shopGroupFile struct {
	Slots            []string                       `json:"slots"`
	CurrentOutfitID  string                         `json:"current_outfit_id"`
	SlotsRefreshDay  string                         `json:"slots_refresh_day"`
	UpdatedAt        string                         `json:"updated_at"`
	UserWarehouses   map[string][]shopWarehouseItem `json:"user_warehouses"`
}

type OutfitShopService struct {
	dir              string
	groupFeaturesPath string
}

func NewOutfitShopService(dir, groupFeaturesPath string) *OutfitShopService {
	return &OutfitShopService{dir: dir, groupFeaturesPath: groupFeaturesPath}
}

// groupNames 复用群功能开关里的群名，避免在商店页显示裸群号。
func (s *OutfitShopService) groupNames() map[string]string {
	out := map[string]string{}
	data, err := os.ReadFile(s.groupFeaturesPath)
	if err != nil {
		return out
	}
	var file struct {
		Groups map[string]struct {
			Name string `json:"name"`
		} `json:"groups"`
	}
	if err := json.Unmarshal(data, &file); err != nil {
		return out
	}
	for gid, g := range file.Groups {
		if g.Name != "" {
			out[gid] = g.Name
		}
	}
	return out
}

func (s *OutfitShopService) loadCatalog() ([]OutfitCatalogItem, error) {
	path := filepath.Join(s.dir, "catalog.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []OutfitCatalogItem{}, nil
		}
		return nil, err
	}
	var file shopCatalogFile
	if err := json.Unmarshal(data, &file); err != nil {
		return nil, err
	}
	items := make([]OutfitCatalogItem, 0, len(file.Items))
	for _, it := range file.Items {
		thumb := ""
		// 缩略图按 <id>.png 存放，缺失就不给前端留坏链接
		if _, err := os.Stat(filepath.Join(s.dir, "thumbs", it.ID+".png")); err == nil {
			thumb = it.ID + ".png"
		}
		items = append(items, OutfitCatalogItem{
			ID:       it.ID,
			Name:     it.Name,
			Price:    it.Price,
			OutfitEN: it.OutfitEN,
			Thumb:    thumb,
		})
	}
	return items, nil
}

// Get 返回货架快照：目录 + 每个群的今日货架 / 当前穿着 / 已购统计。
func (s *OutfitShopService) Get() (OutfitShopSnapshot, error) {
	snap := OutfitShopSnapshot{
		Catalog:     []OutfitCatalogItem{},
		Groups:      []ShopGroup{},
		ThumbDir:    filepath.Join(s.dir, "thumbs"),
		CatalogPath: filepath.Join(s.dir, "catalog.json"),
	}
	catalog, err := s.loadCatalog()
	if err != nil {
		return snap, err
	}
	snap.Catalog = catalog

	nameByID := make(map[string]string, len(catalog))
	for _, it := range catalog {
		nameByID[it.ID] = it.Name
	}

	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return snap, nil
		}
		return snap, err
	}
	names := s.groupNames()
	for _, e := range entries {
		if e.IsDir() || !strings.HasPrefix(e.Name(), "group_") || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		gid := strings.TrimSuffix(strings.TrimPrefix(e.Name(), "group_"), ".json")
		data, err := os.ReadFile(filepath.Join(s.dir, e.Name()))
		if err != nil {
			continue
		}
		var raw shopGroupFile
		if err := json.Unmarshal(data, &raw); err != nil {
			continue
		}
		owned := map[string]struct{}{}
		for _, items := range raw.UserWarehouses {
			for _, it := range items {
				if it.ItemID != "" {
					owned[it.ItemID] = struct{}{}
				}
			}
		}
		slots := make([]OutfitSlot, 0, len(raw.Slots))
		for _, id := range raw.Slots {
			slots = append(slots, OutfitSlot{ID: id, Name: outfitName(nameByID, id)})
		}
		snap.Groups = append(snap.Groups, ShopGroup{
			GroupID:            gid,
			Name:               names[gid],
			Slots:              slots,
			CurrentOutfitID:    raw.CurrentOutfitID,
			CurrentOutfitName:  outfitName(nameByID, raw.CurrentOutfitID),
			SlotsRefreshDay:    raw.SlotsRefreshDay,
			UpdatedAt:          raw.UpdatedAt,
			WarehouseUserCount: len(raw.UserWarehouses),
			OwnedCount:         len(owned),
		})
	}
	sort.Slice(snap.Groups, func(i, j int) bool { return snap.Groups[i].GroupID < snap.Groups[j].GroupID })
	return snap, nil
}

// outfitName：目录里没有的 id 原样返回，方便发现「衣柜里有但目录里没有」的脏数据。
func outfitName(nameByID map[string]string, id string) string {
	if id == "" {
		return ""
	}
	if name, ok := nameByID[id]; ok {
		return name
	}
	return id
}

// ThumbPath 校验并返回缩略图绝对路径；只接受纯文件名，防止目录穿越。
func (s *OutfitShopService) ThumbPath(name string) (string, error) {
	if name == "" || strings.ContainsAny(name, "/\\") || strings.Contains(name, "..") {
		return "", os.ErrNotExist
	}
	path := filepath.Join(s.dir, "thumbs", name)
	if _, err := os.Stat(path); err != nil {
		return "", err
	}
	return path, nil
}
