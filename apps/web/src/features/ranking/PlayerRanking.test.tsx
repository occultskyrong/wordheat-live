import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { DisplayView } from '../../api/fixture/types';
import { PlayerRanking } from './PlayerRanking';

describe('玩家榜公开身份', () => {
  it('同名玩家使用不同稳定标识且不会产生重复 key 警告', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const players = [
      { id: 'participant_demo_1', name: '同名观众', points: 120 },
      { id: 'participant_demo_2', name: '同名观众', points: 96 },
    ] as DisplayView['playerRanking'];
    render(<PlayerRanking players={players} />);
    expect(screen.getAllByText(/同名观众/)).toHaveLength(2);
    expect(screen.getByText('120')).toBeInTheDocument();
    expect(screen.getByText('96')).toBeInTheDocument();
    expect(error.mock.calls.flat().join(' ')).not.toContain('same key');
    error.mockRestore();
  });
});
