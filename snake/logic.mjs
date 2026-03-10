export const DIRECTIONS = Object.freeze({
  UP: Object.freeze({ x: 0, y: -1 }),
  DOWN: Object.freeze({ x: 0, y: 1 }),
  LEFT: Object.freeze({ x: -1, y: 0 }),
  RIGHT: Object.freeze({ x: 1, y: 0 }),
});

const DEFAULT_GRID_SIZE = 16;
const DEFAULT_SNAKE = Object.freeze([
  Object.freeze({ x: 4, y: 8 }),
  Object.freeze({ x: 3, y: 8 }),
  Object.freeze({ x: 2, y: 8 }),
]);

function clonePosition(position) {
  return { x: position.x, y: position.y };
}

function samePosition(a, b) {
  return a.x === b.x && a.y === b.y;
}

function isOppositeDirection(current, next) {
  return current.x + next.x === 0 && current.y + next.y === 0;
}

function isDirection(direction) {
  const magnitude = Math.abs(direction?.x ?? 0) + Math.abs(direction?.y ?? 0);
  return magnitude === 1;
}

export function spawnFood(snake, gridSize, rng = Math.random) {
  const occupied = new Set(snake.map((segment) => `${segment.x},${segment.y}`));
  const freeCells = [];

  for (let y = 0; y < gridSize; y += 1) {
    for (let x = 0; x < gridSize; x += 1) {
      const key = `${x},${y}`;
      if (!occupied.has(key)) {
        freeCells.push({ x, y });
      }
    }
  }

  if (freeCells.length === 0) {
    return null;
  }

  const randomValue = rng();
  const index = Math.min(
    freeCells.length - 1,
    Math.max(0, Math.floor(randomValue * freeCells.length)),
  );
  return freeCells[index];
}

export function createInitialState(options = {}) {
  const gridSize = options.gridSize ?? DEFAULT_GRID_SIZE;
  const snake = (options.snake ?? DEFAULT_SNAKE).map(clonePosition);
  const direction = clonePosition(options.direction ?? DIRECTIONS.RIGHT);
  const rng = options.rng ?? Math.random;
  const hasFood = Object.prototype.hasOwnProperty.call(options, "food");
  const food = hasFood ? options.food : spawnFood(snake, gridSize, rng);

  return {
    gridSize,
    snake,
    direction,
    pendingDirection: null,
    food: food ? clonePosition(food) : null,
    score: 0,
    gameOver: false,
    paused: false,
  };
}

export function queueDirection(state, nextDirection) {
  if (state.gameOver || !isDirection(nextDirection) || state.pendingDirection) {
    return state;
  }

  if (isOppositeDirection(state.direction, nextDirection)) {
    return state;
  }

  if (samePosition(state.direction, nextDirection)) {
    return state;
  }

  return {
    ...state,
    pendingDirection: clonePosition(nextDirection),
  };
}

export function togglePause(state) {
  if (state.gameOver) {
    return state;
  }

  return {
    ...state,
    paused: !state.paused,
  };
}

export function restartGame(state, rng = Math.random) {
  return createInitialState({
    gridSize: state.gridSize,
    rng,
  });
}

function isOutOfBounds(position, gridSize) {
  return (
    position.x < 0 ||
    position.y < 0 ||
    position.x >= gridSize ||
    position.y >= gridSize
  );
}

export function stepGame(state, rng = Math.random) {
  if (state.gameOver || state.paused) {
    return state;
  }

  const direction = state.pendingDirection ?? state.direction;
  const currentHead = state.snake[0];
  const nextHead = {
    x: currentHead.x + direction.x,
    y: currentHead.y + direction.y,
  };

  if (isOutOfBounds(nextHead, state.gridSize)) {
    return {
      ...state,
      direction,
      pendingDirection: null,
      gameOver: true,
      paused: false,
    };
  }

  const isEating = Boolean(state.food && samePosition(nextHead, state.food));
  const collisionBody = isEating ? state.snake : state.snake.slice(0, -1);
  const hitsSelf = collisionBody.some((segment) => samePosition(segment, nextHead));

  if (hitsSelf) {
    return {
      ...state,
      direction,
      pendingDirection: null,
      gameOver: true,
      paused: false,
    };
  }

  const snake = [nextHead, ...state.snake];
  if (!isEating) {
    snake.pop();
  }

  let score = state.score;
  let food = state.food;
  let gameOver = false;

  if (isEating) {
    score += 1;
    food = spawnFood(snake, state.gridSize, rng);
    if (!food) {
      gameOver = true;
    }
  }

  return {
    ...state,
    snake,
    direction,
    pendingDirection: null,
    food,
    score,
    gameOver,
    paused: false,
  };
}
