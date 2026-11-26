/* Naver Maps JS v3 behind the same calls the page already makes for Leaflet.
   Trip lines, charge dots, arrows, and the location pin keep working.
   If the script never loads, the page stays on Leaflet. */
(function (root) {
  function asLeaflet(naver) {
    var maps = naver.maps;

    function ll(pt) {
      if (pt == null) return null;
      if (pt instanceof maps.LatLng) return pt;
      if (Array.isArray(pt)) return new maps.LatLng(Number(pt[0]), Number(pt[1]));
      var lat = pt.lat;
      var lng = pt.lng != null ? pt.lng : pt.lon;
      if (typeof lat === "function") lat = lat();
      if (typeof lng === "function") lng = lng();
      return new maps.LatLng(Number(lat), Number(lng));
    }

    function plain(latlng) {
      if (!latlng) return { lat: 0, lng: 0 };
      var lat = typeof latlng.lat === "function" ? latlng.lat() : latlng.lat;
      var lng = typeof latlng.lng === "function" ? latlng.lng() : (latlng.lng != null ? latlng.lng : latlng.lon);
      return { lat: Number(lat), lng: Number(lng) };
    }

    function Group() {
      this.items = [];
      this.map = null;
    }
    Group.prototype.addTo = function (map) {
      this.map = map;
      this.items.forEach(function (item) { item._attach(map); });
      return this;
    };
    Group.prototype.addLayer = function (item) {
      this.items.push(item);
      if (this.map) item._attach(this.map);
    };
    Group.prototype.clearLayers = function () {
      this.items.forEach(function (item) { item._detach(); });
      this.items = [];
    };
    Group.prototype.remove = function () {
      this.clearLayers();
      this.map = null;
    };

    function Overlay(kind, raw) {
      this._kind = kind;
      this._raw = raw;
      this._map = null;
      this._popup = "";
      this._style = {};
    }
    Overlay.prototype._attach = function (map) {
      this._map = map;
      if (this._raw && this._raw.setMap) this._raw.setMap(map._raw || map);
    };
    Overlay.prototype._detach = function () {
      if (this._raw && this._raw.setMap) this._raw.setMap(null);
    };
    Overlay.prototype.addTo = function (target) {
      if (target && target.addLayer) target.addLayer(this);
      else this._attach(target);
      return this;
    };
    Overlay.prototype.bringToFront = function () {
      if (this._raw && this._raw.setZIndex) this._raw.setZIndex(1000);
      return this;
    };
    Overlay.prototype.getLatLng = function () {
      if (this._raw && this._raw.getPosition) return plain(this._raw.getPosition());
      return plain(this._at);
    };
    Overlay.prototype.setLatLng = function (pt) {
      this._at = ll(pt);
      if (this._raw && this._raw.setPosition) this._raw.setPosition(this._at);
      return this;
    };
    Overlay.prototype.setOpacity = function (value) {
      var el = this._raw && this._raw.getElement && this._raw.getElement();
      if (el) el.style.opacity = String(value);
      return this;
    };
    Overlay.prototype.on = function (name, fn) {
      var self = this;
      maps.Event.addListener(this._raw, name, function (ev) {
        fn({
          latlng: ev && ev.coord ? plain(ev.coord) : self.getLatLng(),
          originalEvent: ev && (ev.pointerEvent || ev.domEvent),
          domEvent: ev && (ev.pointerEvent || ev.domEvent)
        });
      });
      return this;
    };
    Overlay.prototype.bindPopup = function (html) {
      this._popup = html || "";
      return this;
    };
    Overlay.prototype.openPopup = function (pt) {
      if (!this._map || !maps.InfoWindow) return this;
      if (!this._info) this._info = new maps.InfoWindow({ content: this._popup });
      else if (this._info.setContent) this._info.setContent(this._popup);
      var at = pt ? ll(pt) : (this._raw.getPosition ? this._raw.getPosition() : this._at);
      this._info.open(this._map._raw, at);
      return this;
    };
    Overlay.prototype.bindTooltip = function (html) {
      this._tip = html || "";
      var self = this;
      maps.Event.addListener(this._raw, "click", function () { self.openTooltip(); });
      return this;
    };
    Overlay.prototype.openTooltip = function () {
      var html = this._tip || this._popup;
      if (!html) return this;
      this._popup = html;
      return this.openPopup(this.getLatLng());
    };

    function strokeOf(style) {
      return {
        strokeColor: style.color || "#5b9fd6",
        strokeWeight: style.weight || 4,
        strokeOpacity: style.opacity == null ? 0.9 : style.opacity,
        strokeStyle: style.dashArray ? "shortdash" : "solid",
        clickable: style.interactive !== false && style.opacity !== 0
      };
    }

    function polyline(latlngs, style) {
      var path = (latlngs || []).map(ll);
      var raw = new maps.Polyline(Object.assign({ path: path }, strokeOf(style || {})));
      var line = new Overlay("line", raw);
      line._style = Object.assign({}, style || {});
      line.setStyle = function (next) {
        Object.assign(line._style, next || {});
        raw.setOptions(strokeOf(line._style));
        return line;
      };
      return line;
    }

    function circleHtml(style) {
      var r = Number(style.radius || 6);
      var d = Math.max(4, r * 2);
      var fill = style.fillColor || style.color || "#5b9fd6";
      var opacity = style.fillOpacity == null ? 1 : style.fillOpacity;
      var border = (style.weight || 1.5) + "px " + (style.dashArray ? "dashed" : "solid") + " " + (style.color || fill);
      return "<div style=\"width:" + d + "px;height:" + d + "px;box-sizing:border-box;border-radius:50%;background:" +
        fill + ";opacity:" + opacity + ";border:" + border + "\"></div>";
    }

    function markerIcon(icon, fallbackHtml) {
      var size = (icon && icon.iconSize) || [18, 18];
      var anchor = (icon && icon.iconAnchor) || [size[0] / 2, size[1] / 2];
      var html = icon && icon.html != null ? icon.html : (fallbackHtml || "");
      var cls = icon && icon.className ? icon.className : "";
      var rot = icon && icon.rotation != null ? Number(icon.rotation) : 0;
      if (!isFinite(rot)) rot = 0;
      var inner = "<div class=\"teslife-rot\" style=\"width:100%;height:100%;display:flex;align-items:center;" +
        "justify-content:center;transform-origin:center center;transform:rotate(" + rot + "deg)\">" + html + "</div>";
      return {
        content: "<div class=\"" + cls + "\" style=\"width:" + size[0] + "px;height:" + size[1] +
          "px;display:flex;align-items:center;justify-content:center;line-height:1\">" + inner + "</div>",
        size: new maps.Size(size[0], size[1]),
        anchor: new maps.Point(anchor[0], anchor[1])
      };
    }

    function circleMarker(pt, style) {
      var spec = Object.assign({ radius: 6 }, style || {});
      var diameter = Math.max(4, Number(spec.radius || 6) * 2);
      var raw = new maps.Marker({
        position: ll(pt),
        icon: markerIcon({ iconSize: [diameter, diameter], iconAnchor: [diameter / 2, diameter / 2] }, circleHtml(spec)),
        clickable: spec.interactive !== false
      });
      var dot = new Overlay("marker", raw);
      dot._style = spec;
      dot._at = ll(pt);
      dot.setStyle = function (next) {
        Object.assign(dot._style, next || {});
        raw.setIcon(markerIcon(null, circleHtml(dot._style)));
        return dot;
      };
      return dot;
    }

    function htmlDot(pt, html, diameter) {
      var d = Number(diameter);
      var icon = {
        content: html,
        size: new naver.maps.Size(d, d),
        anchor: new naver.maps.Point(d / 2, d / 2)
      };
      var raw = new maps.Marker({
        position: ll(pt),
        icon: icon,
        clickable: true
      });
      var dot = new Overlay("marker", raw);
      dot._at = ll(pt);
      dot._diameter = d;
      dot.setHtml = function (nextHtml) {
        raw.setIcon({
          content: nextHtml,
          size: new naver.maps.Size(dot._diameter, dot._diameter),
          anchor: new naver.maps.Point(dot._diameter / 2, dot._diameter / 2)
        });
        return dot;
      };
      return dot;
    }

    function marker(pt, opts) {
      var options = opts || {};
      var icon = options.icon || {};
      var raw = new maps.Marker({
        position: ll(pt),
        icon: markerIcon(icon, ""),
        clickable: options.interactive !== false,
        draggable: !!options.draggable
      });
      var pin = new Overlay("marker", raw);
      pin._at = ll(pt);
      if (options.icon && options.icon.html && !pin._popupBound) {
        pin.bindPopup = Overlay.prototype.bindPopup;
      }
      var baseBind = pin.bindPopup.bind(pin);
      pin.bindPopup = function (html) {
        baseBind(html);
        if (!pin._popupBound) {
          pin._popupBound = true;
          maps.Event.addListener(raw, "click", function () { pin.openPopup(); });
        }
        return pin;
      };
      pin.setIcon = function (next) {
        pin._icon = next || {};
        raw.setIcon(markerIcon(pin._icon, ""));
        if (pin._rotation != null) pin.setRotationAngle(pin._rotation);
        return pin;
      };
      pin.setRotationAngle = function (deg) {
        var angle = Number(deg);
        pin._rotation = isFinite(angle) ? angle : 0;
        var root = raw.getElement && raw.getElement();
        var node = root && (root.querySelector(".car-loc-arrow") || root.querySelector(".teslife-rot"));
        if (node) {
          node.style.transformOrigin = "center center";
          node.style.transform = "rotate(" + pin._rotation + "deg)";
        }
        return pin;
      };
      if (options.zIndexOffset && raw.setZIndex) raw.setZIndex(options.zIndexOffset);
      if (options.draggable) {
        maps.Event.addListener(raw, "drag", function (ev) {
          if (ev && ev.coord) pin._at = ev.coord;
        });
        maps.Event.addListener(raw, "dragend", function (ev) {
          if (ev && ev.coord) pin._at = ev.coord;
        });
      }
      var baseAttach = pin._attach;
      pin._attach = function (map) {
        baseAttach.call(pin, map);
        if (pin._rotation != null) pin.setRotationAngle(pin._rotation);
      };
      return pin;
    }

    function mapCorner(map, position) {
      var host = map._el || (map.getContainer && map.getContainer()) || document.body;
      var found = host.querySelector("[data-teslife-corner='" + position + "']");
      if (found) return found;
      var spot = position || "topright";
      var bottom = spot.indexOf("bottom") === 0;
      var left = spot.indexOf("left") > 0;
      var div = document.createElement("div");
      div.className = "teslife-map-corner";
      div.setAttribute("data-teslife-corner", spot);
      div.style.position = "absolute";
      div.style.zIndex = "800";
      div.style.display = "flex";
      div.style.flexDirection = "column";
      div.style.gap = "8px";
      div.style.pointerEvents = "none";
      div.style.alignItems = left ? "flex-start" : "flex-end";
      div.style[bottom ? "bottom" : "top"] = bottom ? "36px" : "10px";
      div.style[left ? "left" : "right"] = "10px";
      host.appendChild(div);
      return div;
    }

    function control(options) {
      var position = (options && options.position) || "topright";
      return {
        options: { position: position },
        onAdd: function () { return document.createElement("div"); },
        addTo: function (map) {
          var el = this.onAdd(map);
          if (!el) return this;
          el.classList.add("teslife-map-control");
          mapCorner(map, this.options.position || position).appendChild(el);
          this._el = el;
          this._map = map;
          return this;
        },
        remove: function () {
          if (this._el && this._el.parentNode) this._el.parentNode.removeChild(this._el);
          this._el = null;
          return this;
        },
        getContainer: function () { return this._el || null; }
      };
    }

    function divIcon(opts) {
      return Object.assign({ _divIcon: true }, opts || {});
    }

    function create(el, opts) {
      var raw = new maps.Map(el, {
        center: new maps.LatLng(36.5, 127.8),
        zoom: 7,
        zoomControl: !opts || opts.zoomControl !== false
      });
      return {
        _raw: raw,
        _el: el,
        attributionControl: null,
        setView: function (pt, zoom) {
          raw.setCenter(ll(pt));
          if (zoom != null) raw.setZoom(zoom);
        },
        setZoom: function (zoom) { raw.setZoom(zoom); },
        getZoom: function () { return raw.getZoom(); },
        panTo: function (pt) { raw.panTo(ll(pt)); },
        fitBounds: function (bounds, opt) {
          if (!bounds) {
            this.setView([36.5, 127.8], 7);
            return;
          }
          var pad = opt && opt.padding ? opt.padding[0] : 14;
          var box = new maps.LatLngBounds(ll(bounds[0]), ll(bounds[1]));
          raw.fitBounds(box, {
            top: pad, right: pad, bottom: pad, left: pad,
            maxZoom: opt && opt.maxZoom ? opt.maxZoom : 16
          });
        },
        invalidateSize: function () {
          if (raw.autoResize) raw.autoResize();
          if (maps.Event && maps.Event.trigger) maps.Event.trigger(raw, "resize");
        },
        getBounds: function () {
          var box = raw.getBounds();
          return {
            contains: function (pt) {
              return box && box.hasLatLng ? box.hasLatLng(ll(pt)) : true;
            }
          };
        },
        latLngToLayerPoint: function (pt) {
          var p = raw.getProjection().fromCoordToOffset(ll(pt));
          return { x: p.x, y: p.y };
        },
        layerPointToLatLng: function (pt) {
          var coord = raw.getProjection().fromOffsetToCoord(new maps.Point(pt.x, pt.y));
          return plain(coord);
        },
        removeLayer: function (layer) {
          if (!layer) return;
          if (layer._detach) layer._detach();
          else if (layer.remove) layer.remove();
        },
        on: function (name, fn) {
          var ev = name === "zoomend" ? "zoom_changed" : name;
          maps.Event.addListener(raw, ev, function (e) {
            if (name !== "click") {
              fn(e || {});
              return;
            }
            var pe = e && (e.pointerEvent || e.domEvent);
            var point = { x: 0, y: 0 };
            if (pe && el.getBoundingClientRect) {
              var rect = el.getBoundingClientRect();
              point = { x: pe.clientX - rect.left, y: pe.clientY - rect.top };
            } else if (e && e.offset) {
              point = { x: e.offset.x, y: e.offset.y };
            }
            fn({
              latlng: e && e.coord ? plain(e.coord) : { lat: 0, lng: 0 },
              containerPoint: point,
              originalEvent: pe
            });
          });
        }
      };
    }

    return {
      map: create,
      layerGroup: function () { return new Group(); },
      polyline: polyline,
      circleMarker: circleMarker,
      htmlDot: htmlDot,
      marker: marker,
      divIcon: divIcon,
      point: function (x, y) { return { x: x, y: y }; },
      LineUtil: {
        pointToSegmentDistance: function (p, a, b) {
          var dx = b.x - a.x;
          var dy = b.y - a.y;
          var len2 = dx * dx + dy * dy;
          var t = len2 > 0 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2 : 0;
          t = Math.max(0, Math.min(1, t));
          var x = a.x + t * dx;
          var y = a.y + t * dy;
          return Math.hypot(p.x - x, p.y - y);
        }
      },
      control: control,
      DomUtil: {
        create: function (tag, className, parent) {
          var el = document.createElement(tag || "div");
          if (className) el.className = className;
          if (parent) parent.appendChild(el);
          return el;
        }
      },
      DomEvent: {
        stopPropagation: function (ev) {
          var raw = ev && (ev.domEvent || ev.originalEvent || ev.pointerEvent || ev);
          if (raw && raw.stopPropagation) raw.stopPropagation();
        },
        disableClickPropagation: function (el) {
          if (!el || !el.addEventListener) return el;
          ["click", "dblclick", "mousedown", "touchstart", "pointerdown", "contextmenu", "wheel"].forEach(function (name) {
            el.addEventListener(name, function (ev) { ev.stopPropagation(); });
          });
          return el;
        },
        disableScrollPropagation: function (el) {
          if (!el || !el.addEventListener) return el;
          ["wheel", "mousewheel", "touchmove"].forEach(function (name) {
            el.addEventListener(name, function (ev) { ev.stopPropagation(); });
          });
          return el;
        }
      }
    };
  }

  root.TeslifeNaver = { asLeaflet: asLeaflet };
})(typeof window !== "undefined" ? window : globalThis);
