<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency, formatCurrencyWithDecimals } from '../utils/currency'

const { t, currentLocale, currentCurrency, translateProductName } = useI18n()

const budget = ref(100000)
const recommendations = ref(null)
const loading = ref(true)
const error = ref(null)
const submitting = ref(false)
const orderError = ref(null)
const confirmation = ref(null)

const items = computed(() => recommendations.value?.items ?? [])
const money = (value) => formatCurrency(Number(value) || 0, currentCurrency.value)
const moneyExact = (value) => formatCurrencyWithDecimals(Number(value) || 0, currentCurrency.value, 2)

let requestId = 0
let debounceTimer = null

const loadRecommendations = async () => {
  const id = ++requestId
  loading.value = true
  error.value = null
  try {
    const data = await api.getRestockRecommendations(budget.value)
    if (id !== requestId) return
    recommendations.value = data
  } catch (err) {
    if (id !== requestId) return
    error.value = t('restocking.loadFailed')
    console.error('Restock load error:', err)
  } finally {
    if (id === requestId) loading.value = false
  }
}

const onBudgetChange = () => {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(loadRecommendations, 300)
}

const formatDate = (value) => {
  const d = new Date(value)
  if (isNaN(d.getTime())) return String(value ?? '')
  return d.toLocaleDateString(currentLocale.value === 'ja' ? 'ja-JP' : 'en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric'
  })
}

const placeOrder = async () => {
  if (submitting.value || items.value.length === 0) return
  clearTimeout(debounceTimer)
  submitting.value = true
  orderError.value = null
  confirmation.value = null
  try {
    const order = await api.createRestockOrder({
      budget: budget.value,
      items: items.value.map(i => ({ sku: i.sku, quantity: i.order_quantity }))
    })
    confirmation.value = {
      orderNumber: order.order_number,
      expectedDelivery: order.expected_delivery
    }
    await loadRecommendations()
  } catch (err) {
    console.error('Restock order error:', err)
    orderError.value = err.response?.data?.detail || t('restocking.orderFailed')
  } finally {
    submitting.value = false
  }
}

const confirmationText = computed(() => {
  if (!confirmation.value) return ''
  return t('restocking.orderPlaced', {
    orderNumber: confirmation.value.orderNumber,
    date: formatDate(confirmation.value.expectedDelivery)
  })
})

onMounted(loadRecommendations)
onBeforeUnmount(() => clearTimeout(debounceTimer))
</script>

<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div class="card budget-card">
      <div class="budget-row">
        <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
        <span class="budget-value">{{ money(budget) }}</span>
      </div>
      <input
        id="budget-slider"
        v-model.number="budget"
        type="range"
        min="0"
        max="250000"
        step="5000"
        class="budget-slider"
        @input="onBudgetChange"
      />
      <p class="budget-hint">{{ t('restocking.budgetHint') }}</p>
    </div>

    <div v-if="confirmation" class="banner banner-success" role="status">{{ confirmationText }}</div>
    <div v-if="orderError" class="banner banner-error" role="alert">{{ orderError }}</div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-else-if="loading && !recommendations" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="recommendations" :class="{ refreshing: loading }">
      <div class="summary-grid">
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.totalCost') }}</div>
          <div class="stat-value">{{ money(recommendations.total_cost) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
          <div class="stat-value">{{ money(recommendations.remaining_budget) }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.recommendedItems') }}</div>
          <div class="stat-value">{{ items.length }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">
            {{ t('restocking.recommendedItems') }} ({{ t('restocking.itemsCount', { count: items.length }) }})
          </h3>
          <button
            type="button"
            class="place-order"
            :disabled="items.length === 0 || submitting"
            @click="placeOrder"
          >
            {{ submitting ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="items.length === 0" class="empty">{{ t('restocking.noRecommendations') }}</div>
        <div v-else class="table-container">
          <table class="restock-table">
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.itemName') }}</th>
                <th class="num">{{ t('restocking.table.onHand') }}</th>
                <th class="num">{{ t('restocking.table.forecast') }}</th>
                <th class="num">{{ t('restocking.table.orderQty') }}</th>
                <th class="num">{{ t('restocking.table.unitCost') }}</th>
                <th class="num">{{ t('restocking.table.lineTotal') }}</th>
                <th>{{ t('restocking.table.supplier') }}</th>
                <th>{{ t('restocking.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in items" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ translateProductName(item.name) }}</td>
                <td class="num">{{ item.quantity_on_hand.toLocaleString() }}</td>
                <td class="num">{{ item.forecasted_demand.toLocaleString() }}</td>
                <td class="num">{{ item.order_quantity.toLocaleString() }}</td>
                <td class="num">{{ moneyExact(item.unit_cost) }}</td>
                <td class="num"><strong>{{ money(item.line_total) }}</strong></td>
                <td>{{ item.supplier_name }}</td>
                <td>{{ t('restocking.leadTimeDays', { days: item.lead_time_days }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.budget-card {
  padding: 1.25rem 1.5rem;
  margin-bottom: 1.5rem;
}

.budget-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.75rem;
}

.budget-label {
  font-weight: 600;
  color: #0f172a;
}

.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  width: 100%;
  accent-color: #0f172a;
}

.budget-hint {
  margin: 0.5rem 0 0;
  font-size: 0.875rem;
  color: #64748b;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.banner {
  padding: 0.75rem 1rem;
  border-radius: 8px;
  margin-bottom: 1rem;
  font-size: 0.938rem;
  border: 1px solid;
}

.banner-success {
  background: #f0fdf4;
  border-color: #bbf7d0;
  color: #166534;
}

.banner-error {
  background: #fef2f2;
  border-color: #fecaca;
  color: #991b1b;
}

.refreshing {
  opacity: 0.6;
  transition: opacity 0.15s;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.place-order {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.5rem 1.25rem;
  font-weight: 600;
  cursor: pointer;
}

.place-order:hover:not(:disabled) {
  background: #1e293b;
}

.place-order:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.restock-table {
  width: 100%;
}

.num {
  text-align: right;
}

.empty {
  padding: 2rem;
  text-align: center;
  color: #64748b;
}

@media (max-width: 768px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
