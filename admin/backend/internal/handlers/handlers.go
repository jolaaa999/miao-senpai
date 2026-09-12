package handlers

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"github.com/qqbot/dl-senpai-console/internal/config"
	"github.com/qqbot/dl-senpai-console/internal/middleware"
	"github.com/qqbot/dl-senpai-console/internal/services"
	"golang.org/x/crypto/bcrypt"
	"gorm.io/gorm"
)

type Handler struct {
	cfg     config.Config
	db      *gorm.DB
	memory  *services.MemoryService
	checkin   *services.CheckinService
	affection *services.AffectionService
	sticker   *services.StickerService
	draw    *services.DrawService
	trends  *services.TrendsService
	config  *services.ConfigReader
	logs    *services.LogService
	persona *services.PersonaService
	groupFeatures *services.GroupFeaturesService
	speakStyles   *services.SpeakStylesService
}

func New(cfg config.Config, db *gorm.DB) *Handler {
	return &Handler{
		cfg:     cfg,
		db:      db,
		memory:  services.NewMemoryService(cfg.MemoryDir),
		checkin:   services.NewCheckinService(cfg.CheckinDir),
		affection: services.NewAffectionService(cfg.AffectionDir),
		sticker:   services.NewStickerService(cfg.StickerDir),
		draw:    services.NewDrawService(cfg.DrawDir),
		trends:  services.NewTrendsService(cfg.TrendsDir),
		config:  services.NewConfigReader(cfg.ProjectRoot),
		logs:    services.NewLogService(cfg.ProjectRoot),
		persona: services.NewPersonaService(cfg.ProjectRoot),
		groupFeatures: services.NewGroupFeaturesService(cfg.GroupFeaturesPath),
		speakStyles:   services.NewSpeakStylesService(cfg.ProjectRoot),
	}
}

type loginReq struct {
	Username string `json:"username" binding:"required"`
	Password string `json:"password" binding:"required"`
}

func (h *Handler) Login(c *gin.Context) {
	var req loginReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	ok := req.Username == h.cfg.AdminUser && req.Password == h.cfg.AdminPass
	if !ok {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "用户名或密码错误"})
		return
	}
	exp := time.Now().Add(24 * time.Hour)
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, &middleware.Claims{
		Username: req.Username,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(exp),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
		},
	})
	signed, err := token.SignedString([]byte(h.cfg.TokenSecret))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "token error"})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"token":      signed,
		"expires_at": exp.Format(time.RFC3339),
		"username":   req.Username,
	})
}

func HashPassword(pw string) (string, error) {
	b, err := bcrypt.GenerateFromPassword([]byte(pw), bcrypt.DefaultCost)
	return string(b), err
}

func (h *Handler) OverviewPulse(c *gin.Context) {
	sessions, _ := h.memory.ListSessions()
	groups, _ := h.checkin.ListGroups()
	affGroups, _ := h.affection.ListGroups()
	stickerSessions, _ := h.sticker.ListSessions()
	drawSessions, _ := h.draw.ListSessions()
	trends, _ := h.trends.GetSnapshot()
	features := h.config.GetFeatureSwitches()
	speakSnap, _ := h.speakStyles.Snapshot()

	totalUsers := 0
	for _, g := range groups {
		totalUsers += g.UserCount
	}
	totalAffectionUsers := 0
	for _, g := range affGroups {
		totalAffectionUsers += g.UserCount
	}
	totalStickers := 0
	for _, ss := range stickerSessions {
		totalStickers += ss.ItemCount
	}
	totalDraws := 0
	for _, ds := range drawSessions {
		if ds.SessionID != "__all_files__" {
			totalDraws += ds.ItemCount
		}
	}

	enabledCount := 0
	for _, f := range features {
		if f.Value == "true" || f.Value == "1" {
			enabledCount++
		}
	}

	styleID, styleName, styleCount := "", "", 0
	if speakSnap != nil {
		styleID = speakSnap.ActiveID
		styleName = speakSnap.Active.Name
		styleCount = len(speakSnap.Styles)
	}

	c.JSON(http.StatusOK, gin.H{
		"session_count":           len(sessions),
		"checkin_group_count":     len(groups),
		"checkin_user_count":      totalUsers,
		"affection_group_count":   len(affGroups),
		"affection_user_count":    totalAffectionUsers,
		"sticker_count":           totalStickers,
		"draw_count":              totalDraws,
		"trends_item_count":       trends.ItemCount,
		"trends_fetched_at":       trends.FetchedAt,
		"feature_count":           len(features),
		"features_enabled":        enabledCount,
		"recent_sessions":         firstN(sessions, 5),
		"project_root":            h.cfg.ProjectRoot,
		"speak_style_active_id":   styleID,
		"speak_style_active_name": styleName,
		"speak_style_count":       styleCount,
	})
}

func firstN[T any](s []T, n int) []T {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func (h *Handler) ListSessions(c *gin.Context) {
	sessions, err := h.memory.ListSessions()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": sessions})
}

func (h *Handler) GetTurns(c *gin.Context) {
	sid := c.Param("sid")
	turns, err := h.memory.GetTurns(sid)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "session not found"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"session_id": sid, "turns": turns})
}

func (h *Handler) ClearTurns(c *gin.Context) {
	sid := c.Param("sid")
	if err := h.memory.ClearSession(sid); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ok": true})
}

func (h *Handler) ListCheckinGroups(c *gin.Context) {
	groups, err := h.checkin.ListGroups()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": groups})
}

func (h *Handler) ListCheckinUsers(c *gin.Context) {
	gid := c.Param("gid")
	users, err := h.checkin.GetUsers(gid)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"group_id": gid, "items": users})
}

func (h *Handler) PatchCheckinUser(c *gin.Context) {
	gid := c.Param("gid")
	uid := c.Param("uid")
	var patch services.CheckinUserPatch
	if err := c.ShouldBindJSON(&patch); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	user, err := h.checkin.PatchUser(gid, uid, patch)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, user)
}

func (h *Handler) ListGroupFeatures(c *gin.Context) {
	items, err := h.groupFeatures.List()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items, "features": services.GroupFeatureCatalog})
}

func (h *Handler) PatchGroupFeature(c *gin.Context) {
	gid := c.Param("gid")
	var patch services.GroupFeaturePatch
	if err := c.ShouldBindJSON(&patch); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	entry, err := h.groupFeatures.Patch(gid, patch.Feature, patch.Value)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, entry)
}

func (h *Handler) ListAffectionGroups(c *gin.Context) {
	groups, err := h.affection.ListGroups()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": groups})
}

func (h *Handler) ListAffectionUsers(c *gin.Context) {
	gid := c.Param("gid")
	users, err := h.affection.GetUsers(gid)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"group_id": gid, "items": users})
}

func (h *Handler) PatchAffectionUser(c *gin.Context) {
	gid := c.Param("gid")
	uid := c.Param("uid")
	var patch services.AffectionUserPatch
	if err := c.ShouldBindJSON(&patch); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	user, err := h.affection.PatchUser(gid, uid, patch)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, user)
}

func (h *Handler) ListStickerSessions(c *gin.Context) {
	items, err := h.sticker.ListSessions()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (h *Handler) ListStickerItems(c *gin.Context) {
	sid := c.Param("sid")
	items, err := h.sticker.GetItems(sid)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "session not found"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"session_id": sid, "items": items})
}

func (h *Handler) DeleteStickerItem(c *gin.Context) {
	sid := c.Param("sid")
	id := c.Param("id")
	if err := h.sticker.DeleteItem(sid, id); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ok": true})
}

func (h *Handler) ServeStickerAsset(c *gin.Context) {
	name := c.Param("name")
	path, err := h.sticker.AssetPath(name)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
		return
	}
	c.File(path)
}

func (h *Handler) ListDrawSessions(c *gin.Context) {
	items, err := h.draw.ListSessions()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (h *Handler) ListDrawItems(c *gin.Context) {
	sid := c.Param("sid")
	items, err := h.draw.GetItems(sid)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "session not found"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"session_id": sid, "items": items})
}

func (h *Handler) DeleteDrawItem(c *gin.Context) {
	sid := c.Param("sid")
	id := c.Param("id")
	if err := h.draw.DeleteItem(sid, id); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ok": true})
}

func (h *Handler) ServeDrawAsset(c *gin.Context) {
	name := c.Param("name")
	path, err := h.draw.AssetPath(name)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
		return
	}
	c.File(path)
}

func (h *Handler) FeatureSwitches(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"items": h.config.GetFeatureSwitches()})
}

func (h *Handler) TrendsSnapshot(c *gin.Context) {
	snap, err := h.trends.GetSnapshot()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, snap)
}

func (h *Handler) ListLogStreams(c *gin.Context) {
	items, err := h.logs.ListFiles()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if items == nil {
		items = []services.LogFileInfo{}
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

func (h *Handler) TailLogStream(c *gin.Context) {
	id := c.Param("id")
	tail := 300
	if v := c.Query("tail"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			tail = n
		}
	}
	result, err := h.logs.Tail(id, tail)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "log not found"})
		return
	}
	c.JSON(http.StatusOK, result)
}

func (h *Handler) GetPersonaDraft(c *gin.Context) {
	draft, err := h.persona.GetDraft()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, draft)
}

func (h *Handler) PutPersonaDraft(c *gin.Context) {
	var patch services.PersonaPatch
	if err := c.ShouldBindJSON(&patch); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	if err := h.persona.ApplyPatch(patch); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	draft, err := h.persona.GetDraft()
	if err != nil {
		c.JSON(http.StatusOK, gin.H{"ok": true})
		return
	}
	c.JSON(http.StatusOK, draft)
}

func (h *Handler) GetSpeakStyles(c *gin.Context) {
	snap, err := h.speakStyles.Snapshot()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, snap)
}

func (h *Handler) PutSpeakStyleActive(c *gin.Context) {
	var req struct {
		ID string `json:"id" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	snap, err := h.speakStyles.SetActive(req.ID)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, snap)
}

func (h *Handler) UpsertSpeakStyle(c *gin.Context) {
	var st services.SpeakStyle
	if err := c.ShouldBindJSON(&st); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	snap, err := h.speakStyles.Upsert(st)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, snap)
}

func (h *Handler) DeleteSpeakStyle(c *gin.Context) {
	id := c.Param("id")
	snap, err := h.speakStyles.Delete(id)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, snap)
}

func (h *Handler) RegisterRoutes(r *gin.Engine) {
	api := r.Group("/x7k9-dl-senpai-console/v1")
	api.POST("/auth/token", h.Login)

	auth := api.Group("")
	auth.Use(middleware.JWTAuth(h.cfg.TokenSecret))
	{
		auth.GET("/overview-pulse", h.OverviewPulse)
		auth.GET("/senpai-sessions", h.ListSessions)
		auth.GET("/senpai-sessions/:sid/turns", h.GetTurns)
		auth.DELETE("/senpai-sessions/:sid/turns", h.ClearTurns)
		auth.GET("/checkin-roster", h.ListCheckinGroups)
		auth.GET("/checkin-roster/:gid/users", h.ListCheckinUsers)
		auth.PATCH("/checkin-roster/:gid/users/:uid", h.PatchCheckinUser)
		auth.GET("/affection-roster", h.ListAffectionGroups)
		auth.GET("/affection-roster/:gid/users", h.ListAffectionUsers)
		auth.PATCH("/affection-roster/:gid/users/:uid", h.PatchAffectionUser)
		auth.GET("/sticker-vault", h.ListStickerSessions)
		auth.GET("/sticker-vault/assets/:name", h.ServeStickerAsset)
		auth.GET("/sticker-vault/:sid/items", h.ListStickerItems)
		auth.DELETE("/sticker-vault/:sid/items/:id", h.DeleteStickerItem)
		auth.GET("/draw-gallery", h.ListDrawSessions)
		auth.GET("/draw-gallery/assets/:name", h.ServeDrawAsset)
		auth.GET("/draw-gallery/:sid/items", h.ListDrawItems)
		auth.DELETE("/draw-gallery/:sid/items/:id", h.DeleteDrawItem)
		auth.GET("/feature-switches", h.FeatureSwitches)
		auth.GET("/group-features", h.ListGroupFeatures)
		auth.PATCH("/group-features/:gid", h.PatchGroupFeature)
		auth.GET("/trends-snapshot", h.TrendsSnapshot)
		auth.GET("/log-streams", h.ListLogStreams)
		auth.GET("/log-streams/:id/lines", h.TailLogStream)
		auth.GET("/senpai-persona-draft", h.GetPersonaDraft)
		auth.PUT("/senpai-persona-draft", h.PutPersonaDraft)
		auth.GET("/speak-styles", h.GetSpeakStyles)
		auth.PUT("/speak-styles/active", h.PutSpeakStyleActive)
		auth.PUT("/speak-styles", h.UpsertSpeakStyle)
		auth.DELETE("/speak-styles/:id", h.DeleteSpeakStyle)
	}
}
