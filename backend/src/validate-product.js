// ============================================================================
// Kiểm tra dữ liệu sản phẩm gửi lên từ trang admin (POST / PUT /api/products).
// Hàm thuần, không đụng DB -> test được bằng `npm test`.
//
// Giới hạn bám theo kiểu cột trong db.js:
//   name        VARCHAR(255)
//   price       DECIMAL(12,2)  -> tối đa 9 999 999 999.99
//   stock       INT UNSIGNED   -> 0 .. 4 294 967 295
//   image       VARCHAR(512)
// ============================================================================

const LIMITS = {
  nameMax: 255,
  descriptionMax: 5000,
  priceMax: 9999999999.99,
  stockMax: 4294967295,
  imageMax: 512,
};

// Trả về { value } nếu hợp lệ, hoặc { error } (chuỗi tiếng Việt để hiện lên admin).
function validateProduct(body) {
  const b = body && typeof body === 'object' ? body : {};

  const name = typeof b.name === 'string' ? b.name.trim() : '';
  if (!name) return { error: 'Tên sản phẩm không được để trống' };
  if (name.length > LIMITS.nameMax) return { error: `Tên tối đa ${LIMITS.nameMax} ký tự` };

  const description = b.description === undefined || b.description === null ? '' : b.description;
  if (typeof description !== 'string') return { error: 'Mô tả phải là chữ' };
  if (description.length > LIMITS.descriptionMax) {
    return { error: `Mô tả tối đa ${LIMITS.descriptionMax} ký tự` };
  }

  const price = b.price;
  if (typeof price !== 'number' || !Number.isFinite(price) || price < 0 || price > LIMITS.priceMax) {
    return { error: 'Giá phải là số từ 0 trở lên' };
  }

  const stock = b.stock === undefined ? 0 : b.stock;
  if (!Number.isInteger(stock) || stock < 0 || stock > LIMITS.stockMax) {
    return { error: 'Tồn kho phải là số nguyên từ 0 trở lên' };
  }

  // Ảnh: để trống hoặc là link http(s). Lưu bản đã chuẩn hoá bởi URL parser:
  // ký tự như " < > bị mã hoá thành %22 %3C %3E, nên chèn vào src="" không vỡ HTML.
  let image = b.image === undefined || b.image === null ? '' : b.image;
  if (typeof image !== 'string') return { error: 'Link ảnh phải là chữ' };
  image = image.trim();
  if (image) {
    let url;
    try {
      url = new URL(image);
    } catch {
      return { error: 'Link ảnh không hợp lệ' };
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') {
      return { error: 'Link ảnh phải bắt đầu bằng http:// hoặc https://' };
    }
    image = url.href;
    if (image.length > LIMITS.imageMax) return { error: `Link ảnh tối đa ${LIMITS.imageMax} ký tự` };
  }

  return { value: { name, description, price: Math.round(price * 100) / 100, stock, image } };
}

// id trên URL phải là số nguyên dương (tránh query DB với rác như "abc" hay "1;DROP")
function parseId(raw) {
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 && id <= LIMITS.stockMax ? id : null;
}

module.exports = { validateProduct, parseId, LIMITS };
