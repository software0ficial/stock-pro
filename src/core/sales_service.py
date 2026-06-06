from .database import DatabaseManager
from .stock_service import StockService
import logging
from datetime import datetime

class SalesService:
    """
    Servicio de negocio para la gestión de ventas y control de caja.
    Maneja el flujo desde el carrito hasta la liquidación contable.
    """
    
    def __init__(self, db: DatabaseManager, stock_service: StockService):
        self.db = db
        self.stock_service = stock_service
        self.logger = logging.getLogger("SalesService")

    # --- GESTIÓN DE CAJA (CASH BOX) ---

    def open_cash_box(self, monto_inicial):
        """Inicia el turno de caja con un monto de efectivo inicial."""
        try:
            query = '''
                UPDATE cash_box SET 
                    abierta = true, 
                    efectivo_inicial = %s, 
                    ventas_efectivo = 0, 
                    ventas_digital = 0, 
                    hora_apertura = CURRENT_TIMESTAMP,
                    hora_cierre = NULL,
                    monto_cierre_real = NULL
                WHERE id = 1
            '''
            self.db.execute(query, (monto_inicial,))
            self.logger.info(f"Caja abierta con monto inicial: ${monto_inicial}")
            return {"status": "success", "message": "Caja abierta correctamente."}
        except Exception as e:
            self.logger.error(f"Error opening cash box: {e}")
            return {"status": "error", "message": str(e)}

    def close_cash_box(self, monto_real):
        """Cierra el turno de caja y registra el monto real final."""
        try:
            # Obtener estado actual para auditoría
            estado = self.db.fetch_one("SELECT * FROM cash_box WHERE id = 1")
            if not estado or not estado['abierta']:
                return {"status": "error", "message": "La caja no está abierta."}

            query = '''
                UPDATE cash_box SET 
                    abierta = false, 
                    hora_cierre = CURRENT_TIMESTAMP,
                    monto_cierre_real = %s
                WHERE id = 1
            '''
            self.db.execute(query, (monto_real,))
            
            # Calcular diferencia
            esperado = estado['efectivo_inicial'] + estado['ventas_efectivo']
            diferencia = monto_real - esperado
            
            self.logger.info(f"Caja cerrada. Real: ${monto_real}, Esperado: ${esperado}, Dif: ${diferencia}")
            return {
                "status": "success", 
                "message": "Caja cerrada correctamente.",
                "resumen": {
                    "esperado": esperado,
                    "real": monto_real,
                    "diferencia": diferencia
                }
            }
        except Exception as e:
            self.logger.error(f"Error closing cash box: {e}")
            return {"status": "error", "message": str(e)}

    def get_cash_box_status(self):
        """Retorna el estado actual de la caja."""
        status = self.db.fetch_one("SELECT * FROM cash_box WHERE id = 1")
        return {"status": "success", "data": dict(status) if status else None}

    # --- PROCESO DE VENTAS ---

    def process_sale(self, cliente, items, metodo_pago, paga_con=0, alias=None):
        """
        Procesa una venta completa:
        1. Valida stock.
        2. Calcula totales.
        3. Verifica límites de alias si es transferencia.
        4. Registra venta y detalle.
        5. Actualiza stock y caja.
        """
        try:
            # 1. Calcular Total
            total_venta = 0
            processed_items = []
            
            for item in items:
                # item: {codigo, cantidad (en kg si es peso)}
                res = self.stock_service.get_product(item['codigo'])
                if res["status"] == "error":
                    return {"status": "error", "message": f"Producto {item['codigo']} no encontrado."}
                
                p = res["data"]
                
                # DEBUG: Verificando valores en tiempo real
                print(f"DEBUG STOCK: Producto {p['nombre']} | Disponible: {p['cantidad']} | Solicitado: {item['cantidad']}")

                # VALIDACIÓN DE STOCK: Verificar que haya suficiente cantidad disponible
                if p['cantidad'] < item['cantidad']:
                    return {
                        "status": "error", 
                        "message": f"Stock insuficiente para {p['nombre']}. Disponible: {p['cantidad']}, Solicitado: {item['cantidad']}"
                    }

                subtotal = p['precio'] * item['cantidad']
                total_venta += subtotal
                processed_items.append({
                    "codigo": p['codigo'],
                    "cantidad": item['cantidad'],
                    "subtotal": subtotal
                })

            # 2. Validación de Alias (Límite Inteligente del repo original)
            if metodo_pago == "Transferencia" and alias:
                alias_data = self.db.fetch_one("SELECT * FROM aliases WHERE nombre = %s", (alias,))
                if alias_data:
                    if (alias_data['acumulado'] or 0) + total_venta > alias_data['limite']:
                        return {"status": "error", "message": f"Límite excedido para el alias {alias}."}
                else:
                    return {"status": "error", "message": "Alias no registrado."}

            # 3. Cálculo de Vuelto
            vuelto = 0
            if metodo_pago == "Efectivo":
                if paga_con < total_venta:
                    return {"status": "error", "message": "Monto insuficiente para pago en efectivo."}
                vuelto = paga_con - total_venta

            # 4. Persistencia de la Venta (Transaccional)
            with self.db._get_connection() as conn:
                cursor = conn.cursor()
                
                # Insertar Venta
                cursor.execute('''
                    INSERT INTO sales (total, cliente, metodo_pago, paga_con, vuelto)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (total_venta, cliente, metodo_pago, paga_con, vuelto))
                sale_id = cursor.lastrowid
                
                # Insertar Detalle
                for pi in processed_items:
                    cursor.execute('''
                        INSERT INTO sale_items (sale_id, product_codigo, cantidad, subtotal)
                        VALUES (%s, %s, %s, %s)
                    ''', (sale_id, pi['codigo'], pi['cantidad'], pi['subtotal']))
                
                # Actualizar Caja
                if metodo_pago == "Efectivo":
                    cursor.execute("UPDATE cash_box SET ventas_efectivo = ventas_efectivo + %s WHERE id = 1", (total_venta,))
                else:
                    cursor.execute("UPDATE cash_box SET ventas_digital = ventas_digital + %s WHERE id = 1", (total_venta,))
                
                # Actualizar Alias si aplica
                if metodo_pago == "Transferencia" and alias:
                    cursor.execute("UPDATE aliases SET acumulado = acumulado + %s WHERE nombre = %s", (total_venta, alias))
                
                conn.commit()

            # 5. Actualizar Stock (Llamada al StockService)
            for pi in processed_items:
                # Restamos la cantidad vendida
                self.stock_service.update_stock(pi['codigo'], -pi['cantidad'])

            self.logger.info(f"Venta completada: ID {sale_id} | Total: ${total_venta} | Método: {metodo_pago}")
            return {
                "status": "success", 
                "message": "Venta procesada exitosamente.",
                "sale_id": sale_id,
                "vuelto": vuelto
            }

        except Exception as e:
            self.logger.error(f"Critical error processing sale: {e}")
            return {"status": "error", "message": str(e)}

    # --- GESTIÓN DE ALIAS ---

    def add_alias(self, nombre, limite):
        """Crea un nuevo alias con un límite de crédito/recaudación."""
        try:
            # Generar un ID simple
            import uuid
            alias_id = str(uuid.uuid4())[:8]
            self.db.execute("INSERT INTO aliases (id, nombre, limite, acumulado) VALUES (%s, %s, %s, 0)", (alias_id, nombre, limite))
            return {"status": "success", "message": f"Alias {nombre} creado."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def delete_alias(self, alias_id):
        """Elimina un alias del sistema."""
        self.db.execute("DELETE FROM aliases WHERE id = %s", (alias_id,))
        return {"status": "success", "message": "Alias eliminado."}

    def list_aliases(self):
        """Retorna todos los alias y sus consumos."""
        aliases = self.db.fetch_all("SELECT * FROM aliases")
        return {"status": "success", "data": [dict(a) for a in aliases]}

